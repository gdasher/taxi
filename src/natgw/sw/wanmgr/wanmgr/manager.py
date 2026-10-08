# SPDX-License-Identifier: BSD-3-Clause
"""The WAN manager's control loop.

Every round it reconciles VPP with what the WANs' leases and health call
for; it never assumes a change it made earlier is still there after a
reconnect (VPP may have restarted, losing API routes):

  * DHCP clients exist on the WAN interfaces (when manage_dhcp is set)
  * each probe target has a /32 route via its own WAN's gateway, so a probe
    tests that WAN only
  * the default route from the API source (which outranks DHCP's) holds the
    healthy WANs' gateways, set as a whole path set at once; when no WAN is
    healthy it is removed and DHCP's own routes (all WANs) take over
  * every WAN's lease address is in the NAT pool (pool addresses carry a
    VRF so nat44-ed matches each to its WAN). VPP's own probes leave through
    NAT's output feature too, so a WAN without its address in the pool could
    not be probed back up. When a WAN goes down while another is healthy,
    its sessions (and with them any offloaded flows) are flushed by removing
    and re-adding its address, so clients reconnect through a working WAN

Nothing is changed until every WAN with a lease has a health verdict, so a
restart probes before it steers."""

import dataclasses
import json
import logging
import os
import time

from .health import Health, State

log = logging.getLogger("wanmgr")

DEFAULT = "0.0.0.0/0"


class VppDisconnected(Exception):
    """the API connection was lost (VPP stopped or restarted)"""


@dataclasses.dataclass(frozen=True)
class Lease:
    address: str
    prefix_len: int
    gateway: str


class Wan:
    def __init__(self, cfg, probe_cfg):
        self.lost_alerted = False   # a lease-lost alert is outstanding
        self.needs_flush = False    # down, sessions not yet moved off it
        self.cfg = cfg
        self.health = Health(probe_cfg.up_rounds, probe_cfg.down_rounds, probe_cfg.initial_rounds)
        self.sw_if_index = None
        self.lease = None
        self.pinned = {}          # target -> gateway it is routed via
        self.last_probe = None    # target that answered last, or None

    @property
    def name(self):
        return self.cfg.name

    @property
    def state(self):
        return self.health.state


class WanManager:
    def __init__(self, cfg, vpp, alerter, clock=time.monotonic, sleep=time.sleep):
        self.cfg = cfg
        self.vpp = vpp
        self.alerter = alerter
        self.clock = clock
        self.sleep = sleep
        self.wans = [Wan(w, cfg.probe) for w in cfg.wans]
        self.connected = False
        self.ever_connected = False
        self.route_applied = None   # None: unknown; [] removed; else paths
        self.pool_owned = set()     # pool addresses this manager added
        self.connected_at = 0.0
        self.rounds = 0
        self.rounds_connected = 0   # rounds since the last (re)connect
        self.status_file = None

    # ---------------------------------------------------------------- loop

    def run(self, stop=lambda: False):
        backoff = 0.25
        next_round = self.clock()
        while not stop():
            if not self.connected:
                try:
                    self.connect()
                    backoff = 0.25
                except Exception as e:
                    log.debug("connect failed: %s", e)
                    self.sleep(backoff)
                    backoff = min(backoff * 2, 2.0)
                    continue
            try:
                self.round()
            except VppDisconnected as e:
                self._lost(e)
                continue
            except Exception:
                # e.g. an API error while VPP is still being configured: what
                # was not applied is retried next round
                log.exception("round %d failed", self.rounds)
            self.alerter.flush()
            next_round += self.cfg.probe.interval
            delay = next_round - self.clock()
            if delay < 0:
                next_round = self.clock()
            else:
                self.sleep(delay)

    def connect(self):
        self.vpp.connect()
        self.connected = True
        try:
            self._on_connect()
        except VppDisconnected as e:
            self._lost(e)
            raise
        if self.ever_connected:
            self.alerter.alert("VPP reconnected", "Reconnected to VPP; re-probing all WANs before steering.")
        self.ever_connected = True

    def _lost(self, e):
        self.connected = False
        try:
            self.vpp.disconnect()
        except Exception:
            pass
        self.alerter.alert("VPP connection lost", f"Lost the VPP API connection ({e}); reconnecting.")

    def _on_connect(self):
        ifs = self.vpp.interfaces()
        clients = self.vpp.dhcp_clients()
        self.rounds_connected = 0
        self.connected_at = self.clock()
        for w in self.wans:
            # relearn leases quietly: a restarted VPP rebinds its DHCP clients
            w.health.reset()
            w.lease = None
            w.needs_flush = False
            w.pinned = {}
            w.sw_if_index = ifs.get(w.cfg.interface)
            if w.sw_if_index is None:
                log.error("wan %s: interface %s not found", w.name, w.cfg.interface)
                continue
            if w.cfg.manage_dhcp and w.sw_if_index not in clients:
                log.info("wan %s: adding DHCP client on %s", w.name, w.cfg.interface)
                self.vpp.add_dhcp_client(w.sw_if_index, w.cfg.hostname)
        # what VPP has now is unknown: re-apply the route once ready, adopt
        # pool addresses that are some WAN's lease
        self.route_applied = None
        leases = {self._lease_of(w, clients) for w in self.wans} - {None}
        self.pool_owned = {a for a in self.vpp.nat_pool() if a in {lease.address for lease in leases}}

    # ---------------------------------------------------------------- round

    def _lease_of(self, w, clients):
        return clients.get(w.sw_if_index) if w.sw_if_index is not None else None

    def round(self):
        self.rounds += 1
        self.rounds_connected += 1
        clients = self.vpp.dhcp_clients()
        for w in self.wans:
            self._update_lease(w, self._lease_of(w, clients))
        for w in self.wans:
            self._pin_probes(w)
        # before probing: probes need their WAN's address in the pool
        self._apply_pool()
        for w in self.wans:
            if w.lease is None:
                if w.state == State.UNKNOWN and self.clock() - self.connected_at < self.cfg.probe.lease_grace:
                    continue  # DHCP may still be binding
                prev = w.health.no_lease()
            else:
                prev = w.health.round(self._probe(w))
            if prev is not None:
                self._transition(w, prev)
        self._apply_route()
        self._flush_pending()
        self._write_status()

    def _update_lease(self, w, lease):
        if lease == w.lease:
            return
        old, w.lease = w.lease, lease
        if old is None:
            # a WAN that was down only for want of a lease gets a fresh verdict
            w.health.reset()
            log.info("wan %s: lease %s/%s via %s", w.name, lease.address, lease.prefix_len, lease.gateway)
            if w.lost_alerted:
                w.lost_alerted = False
                self.alerter.alert(f"{w.name} lease acquired",
                                   f"{w.name} has address {lease.address}/{lease.prefix_len} via {lease.gateway}.")
        elif lease is None:
            w.lost_alerted = True
            self.alerter.alert(f"{w.name} lease lost", f"{w.name} lost its lease ({old.address} via {old.gateway}).")
        else:
            self.alerter.alert(f"{w.name} lease changed",
                               f"{w.name}: {old.address} via {old.gateway} -> {lease.address} via {lease.gateway}. "
                               f"Sessions using {old.address} are flushed.")
        # sessions on an old address cannot continue: the pool update
        # removes it, and with it the sessions

    def _pin_probes(self, w):
        for t in w.cfg.probe_targets:
            gw = w.lease.gateway if w.lease else None
            if w.pinned.get(t) == gw:
                continue
            if gw is None:
                self.vpp.route_del(f"{t}/32")
                w.pinned.pop(t, None)
            else:
                self.vpp.route_set(f"{t}/32", [(w.sw_if_index, gw, 1)])
                w.pinned[t] = gw

    def _probe(self, w):
        """one round: targets in order until one answers"""
        for t in w.cfg.probe_targets:
            if self.vpp.ping(t, self.cfg.probe.timeout):
                w.last_probe = t
                return True
        w.last_probe = None
        return False

    def _transition(self, w, prev):
        new = w.state
        log.info("wan %s: %s -> %s", w.name, prev.value, new.value)
        if prev == State.UNKNOWN and new == State.UP:
            return  # (re)start: nothing an operator needs to hear about
        healthy = [x.name for x in self.wans if x.state == State.UP]
        detail = (f"{w.name} ({w.cfg.interface}) is {new.value.upper()} (was {prev.value}).\n"
                  f"Lease: {w.lease.address + ' via ' + w.lease.gateway if w.lease else 'none'}\n"
                  f"Probe targets: {', '.join(w.cfg.probe_targets)}\n"
                  f"Healthy WANs: {', '.join(healthy) or 'none'}")
        if new == State.DOWN and not healthy:
            detail += "\nNo WAN is healthy: falling back to DHCP's routes over every WAN."
        w.needs_flush = new == State.DOWN
        self.alerter.alert(f"{w.name} {new.value.upper()}", detail)

    # ---------------------------------------------------------------- apply

    def ready(self):
        return all(w.state != State.UNKNOWN for w in self.wans if w.lease is not None)

    def desired_paths(self):
        return sorted((w.sw_if_index, w.lease.gateway, w.cfg.weight)
                      for w in self.wans if w.state == State.UP and w.lease is not None)

    def desired_pool(self):
        return {w.lease.address for w in self.wans if w.lease is not None}

    def _apply_pool(self):
        want = self.desired_pool()
        # remove first: a changed lease address frees its sessions before the
        # new address is used
        for a in sorted(self.pool_owned - want):
            log.info("NAT pool: removing %s (sessions using it are deleted)", a)
            self.vpp.nat_pool_del(a, self.cfg.nat_vrf_id)
            self.pool_owned.discard(a)
        for a in sorted(want - self.pool_owned):
            log.info("NAT pool: adding %s", a)
            self.vpp.nat_pool_add(a, self.cfg.nat_vrf_id)
            self.pool_owned.add(a)

    def _apply_route(self):
        if not self.ready():
            return
        paths = self.desired_paths()
        if paths == self.route_applied:
            return
        if paths:
            self.vpp.route_set(DEFAULT, paths)
            log.info("default route via %s", ", ".join(f"{gw}" for _, gw, _ in paths))
        else:
            self.vpp.route_del(DEFAULT)
            log.info("default route override removed (no healthy WAN)")
        self.route_applied = paths

    def _flush_pending(self):
        """move clients off WANs that went down, once some WAN is healthy:
        removing a pool address deletes its sessions; it is re-added at once
        (for probing). Not while no WAN is healthy: there is nowhere better."""
        if not any(w.state == State.UP for w in self.wans):
            return
        for w in self.wans:
            if not w.needs_flush or w.state != State.DOWN:
                continue
            w.needs_flush = False
            if w.lease is None or w.lease.address not in self.pool_owned:
                continue    # no lease: its address already left the pool
            a = w.lease.address
            log.info("wan %s: flushing NAT sessions on %s", w.name, a)
            self.vpp.nat_pool_del(a, self.cfg.nat_vrf_id)
            self.vpp.nat_pool_add(a, self.cfg.nat_vrf_id)

    # ---------------------------------------------------------------- status

    def status(self):
        return {
            "connected": self.connected,
            "rounds": self.rounds,
            "route": [{"sw_if_index": s, "gateway": g, "weight": wt} for s, g, wt in (self.route_applied or [])],
            "route_applied": self.route_applied is not None,
            "nat_pool": sorted(self.pool_owned),
            "wans": {w.name: {"state": w.state.value,
                              "lease": dataclasses.asdict(w.lease) if w.lease else None,
                              "last_probe": w.last_probe} for w in self.wans},
        }

    def _write_status(self):
        if not self.status_file:
            return
        tmp = self.status_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.status(), f, indent=1)
        os.replace(tmp, self.status_file)
