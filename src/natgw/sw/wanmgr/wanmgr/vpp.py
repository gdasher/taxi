# SPDX-License-Identifier: BSD-3-Clause
"""VPP binary API adapter (vpp_papi over the API socket).

Every method raises manager.VppDisconnected when the connection is gone, so
the control loop can reconnect; API errors (retval != 0) raise VppError."""

import glob
import ipaddress
import logging
import os
import queue
import socket
import threading

from .manager import Lease, VppDisconnected

log = logging.getLogger("wanmgr")

FIB_API_PATH_NH_PROTO_IP4 = 0
_NO_LABEL = {"is_uniform": 0, "label": 0, "ttl": 0, "exp": 0}   # fixed-size array: pass all 16
DHCP_CLIENT_STATE_BOUND = 2   # vl_api_dhcp_client_state_t: DISCOVER, REQUEST, BOUND


class VppError(RuntimeError):
    pass


class VppApi:
    def __init__(self, api_socket, api_dir, name="natgw-wanmgr", timeout=5.0):
        self.api_socket = api_socket
        self.api_dir = api_dir
        self.name = name
        self.timeout = timeout
        self.c = None
        self._events = queue.Queue()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ connection

    def connect(self):
        from vpp_papi import VPPApiClient
        files = glob.glob(os.path.join(self.api_dir, "**", "*.api.json"), recursive=True)
        if not files:
            raise VppError(f"no API definitions in {self.api_dir}")
        c = VPPApiClient(apifiles=files, use_socket=True, server_address=self.api_socket,
                         read_timeout=self.timeout)
        c.register_event_callback(self._event)
        try:
            c.connect(self.name)
        except Exception as e:
            raise VppDisconnected(f"connect: {e}") from None
        self.c = c
        log.info("connected to VPP at %s", self.api_socket)

    def disconnect(self):
        c, self.c = self.c, None
        if c:
            try:
                c.disconnect()
            except Exception:
                pass

    def _event(self, name, msg):
        if name == "ping_finished_event":
            self._events.put(msg)

    def _call(self, name, **kw):
        if self.c is None:
            raise VppDisconnected("not connected")
        try:
            r = getattr(self.c.api, name)(**kw)
        except (OSError, socket.error, EOFError) as e:
            raise VppDisconnected(f"{name}: {e}") from None
        except Exception as e:
            # vpp_papi raises its own IO/timeout errors
            if type(e).__name__ in ("VPPIOError", "VppTransportSocketIOError", "VPPRuntimeError") \
                    or "timeout" in str(e).lower():
                raise VppDisconnected(f"{name}: {e}") from None
            raise
        if not isinstance(r, list) and getattr(r, "retval", 0) != 0:
            raise VppError(f"{name}: retval {r.retval}")
        return r

    # ------------------------------------------------------------ queries

    def interfaces(self):
        return {i.interface_name: i.sw_if_index for i in self._call("sw_interface_dump")}

    def dhcp_clients(self):
        """{sw_if_index: Lease or None} for each configured DHCP client"""
        res = {}
        for d in self._call("dhcp_client_dump"):
            lease = None
            le = d.lease
            if int(le.state) == DHCP_CLIENT_STATE_BOUND and not le.is_ipv6:
                addr = str(le.host_address)
                gw = str(le.router_address)
                if addr != "0.0.0.0" and gw != "0.0.0.0":
                    lease = Lease(addr, int(le.mask_width), gw)
            res[d.client.sw_if_index] = lease
        return res

    def nat_pool(self):
        return {str(a.ip_address) for a in self._call("nat44_address_dump")}

    # ------------------------------------------------------------ changes

    def add_dhcp_client(self, sw_if_index, hostname):
        self._call("dhcp_client_config", is_add=True,
                   client={"sw_if_index": sw_if_index, "hostname": hostname, "want_dhcp_event": False,
                           "set_broadcast_flag": True, "pid": os.getpid()})

    def route_set(self, prefix, paths):
        """replace the API-sourced path set of a route in table 0;
        paths: [(sw_if_index, gateway, weight)]"""
        self._call("ip_route_add_del", is_add=True, is_multipath=False,
                   route={"table_id": 0, "prefix": prefix, "n_paths": len(paths),
                          "paths": [{"sw_if_index": s, "table_id": 0, "weight": w, "preference": 0,
                                     "proto": FIB_API_PATH_NH_PROTO_IP4,
                                     "nh": {"address": {"ip4": ipaddress.IPv4Address(gw)}},
                                     "n_labels": 0, "label_stack": [_NO_LABEL] * 16}
                                    for s, gw, w in paths]})

    def route_del(self, prefix):
        """remove the API source from a route (other sources stay)"""
        try:
            self._call("ip_route_add_del", is_add=False, is_multipath=False,
                       route={"table_id": 0, "prefix": prefix, "n_paths": 0, "paths": []})
        except VppError as e:
            log.debug("route_del %s: %s", prefix, e)  # not present

    def nat_pool_add(self, address, vrf_id):
        self._call("nat44_add_del_address_range", first_ip_address=address, last_ip_address=address,
                   vrf_id=vrf_id, is_add=True, flags=0)

    def nat_pool_del(self, address, vrf_id):
        """deleting a pool address deletes the sessions that use it"""
        try:
            self._call("nat44_add_del_address_range", first_ip_address=address, last_ip_address=address,
                       vrf_id=vrf_id, is_add=False, flags=0)
        except VppError as e:
            log.warning("NAT pool del %s: %s", address, e)

    # ------------------------------------------------------------ probing

    def ping(self, address, timeout):
        """one ICMP echo from VPP (routed by VPP's FIB); True on a reply.
        VPP's ping handler holds the API until `timeout` has passed."""
        with self._lock:
            while not self._events.empty():
                self._events.get_nowait()
            self._call("want_ping_finished_events", address=address, repeat=1, interval=timeout)
            try:
                ev = self._events.get(timeout=timeout + self.timeout)
            except queue.Empty:
                raise VppDisconnected("no ping result") from None
            return ev.reply_count > 0
