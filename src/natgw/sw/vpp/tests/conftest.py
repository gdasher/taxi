# SPDX-License-Identifier: BSD-3-Clause
import json
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vppenv  # noqa: E402
from vppenv import Netns, Vpp, wait_for  # noqa: E402

TRAFFIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic.py")

LAN_HOST = "192.168.1.10"
SERVER = "203.0.113.10"
WAN1_ADDR, WAN1_GW = "198.51.100.1", "198.51.100.2"
WAN2_ADDR, WAN2_GW = "198.18.0.1", "198.18.0.2"


class Gateway:
    """VPP as the NAT gateway: NatgwModel0 = LAN, NatgwModel1/2 = WAN1/2, each
    lane's wire a namespace. The server address exists behind both WANs."""

    def __init__(self, tag, bucket_w=10):
        self.vpp = Vpp(name=f"ngw{tag}", bucket_w=bucket_w,
                       offload_conf="enable active-packets 4 sync-interval 0.25")
        self.ns = []
        try:
            self._setup(tag)
        except Exception:
            self.close()
            raise

    def _setup(self, tag):
        v = self.vpp
        self.lan = self._ns(f"ngw{tag}_lan")
        self.wan1 = self._ns(f"ngw{tag}_wan1")
        self.wan2 = self._ns(f"ngw{tag}_wan2")
        self.lan.take(v.lane_tap(0), "eth0", f"{LAN_HOST}/24")
        self.lan.run("ip", "route", "add", "default", "via", "192.168.1.1")
        for ns, addr in ((self.wan1, WAN1_GW), (self.wan2, WAN2_GW)):
            ns.take(v.lane_tap(1 if ns is self.wan1 else 2), "eth0", f"{addr}/24")
            ns.run("ip", "addr", "add", f"{SERVER}/32", "dev", "lo")
        for c in ["set interface state NatgwModel0 up",
                  "set interface state NatgwModel1 up",
                  "set interface state NatgwModel2 up",
                  "set interface ip address NatgwModel0 192.168.1.1/24",
                  f"set interface ip address NatgwModel1 {WAN1_ADDR}/24",
                  f"set interface ip address NatgwModel2 {WAN2_ADDR}/24",
                  f"ip route add 0.0.0.0/0 via {WAN1_GW} NatgwModel1",
                  # NAT only as an output feature on the WANs, no inside
                  # interface: sessions are created after the route lookup,
                  # so each gets its egress WAN's address and later packets
                  # hash (ECMP) on the untranslated header like the first.
                  # Pool addresses need a VRF to be matched to their
                  # interface ('add interface address' ones are not).
                  "nat44 plugin enable sessions 4096",
                  "set interface nat44 out NatgwModel1 output-feature",
                  "set interface nat44 out NatgwModel2 output-feature",
                  f"nat44 add address {WAN1_ADDR} tenant-vrf 0",
                  f"nat44 add address {WAN2_ADDR} tenant-vrf 0"]:
            v.cli(c)
        self.servers = []
        for ns in (self.wan1, self.wan2):
            self.servers.append(self._server(ns, "udp-echo", "--bind", SERVER, "--port", "5000"))
            self.servers.append(self._server(ns, "tcp-server", "--bind", SERVER, "--port", "6000", "--echo"))
        # resolve the neighbours once so the first flows need no retry
        self.lan.run("ping", "-c", "1", "-W", "2", "192.168.1.1", check=False)
        for gw, ns in ((WAN1_GW, self.wan1), (WAN2_GW, self.wan2)):
            ns.run("ping", "-c", "1", "-W", "2", WAN1_ADDR if ns is self.wan1 else WAN2_ADDR, check=False)

    def _ns(self, name):
        ns = Netns(name)
        self.ns.append(ns)
        return ns

    def _server(self, ns, *args):
        p = ns.popen("python3", TRAFFIC, *args)
        line = p.stdout.readline()
        assert line.startswith("READY"), line + p.stderr.read()
        return p

    def close(self):
        for p in getattr(self, "servers", []):
            p.kill()
        for ns in self.ns:
            ns.delete()
        self.vpp.stop()

    # ---- traffic

    def client(self, *args):
        """run traffic.py in the LAN namespace; returns its JSON result"""
        r = self.lan.run("python3", TRAFFIC, *args, timeout=120)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def client_phased(self, args, between):
        """run a phased client; between(phase) is called at each pause"""
        p = self.lan.popen("python3", TRAFFIC, *args)
        phase = 0
        while True:
            line = p.stdout.readline()
            if not line:
                raise AssertionError(f"client failed: {p.stderr.read()}")
            if line.startswith("PAUSE"):
                phase += 1
                between(phase)
                p.stdin.write("\n")
                p.stdin.flush()
                continue
            p.wait(timeout=120)
            return json.loads(line)

    # ---- VPP state

    def cli(self, cmd):
        return self.vpp.cli(cmd)

    def offload(self):
        return self.vpp.offload_counters()

    def offloaded(self):
        return self.offload()["offloaded sessions"]

    def sessions(self):
        """nat44 sessions: list of dicts (inside port, proto, total pkts/bytes, outside addr)"""
        out = self.cli("show nat44 sessions")
        res = []
        for block in re.split(r"\n    i2o ", out)[1:]:
            m = re.match(r"(\S+) proto (\S+) port (\d+)", block)
            o = re.search(r"o2i (\S+) proto \S+ port (\d+)", block)
            t = re.search(r"total pkts (\d+), total bytes (\d+)", block)
            res.append({"in_addr": m.group(1), "proto": m.group(2), "in_port": int(m.group(3)),
                        "out_addr": o.group(1), "out_port": int(o.group(2)),
                        "pkts": int(t.group(1)), "bytes": int(t.group(2))})
        return res

    def xstat(self, name, iface="NatgwModel0"):
        out = self.cli(f"show hardware-interfaces {iface} detail")
        m = re.search(rf"^\s*{name}\s+(\d+)\s*$", out, re.M)
        return int(m.group(1)) if m else None

    def lan_rx(self):
        return self.vpp.if_counters("NatgwModel0")[0]

    def reset(self):
        """back to the base state between tests"""
        self.cli("clear nat44 ed sessions")
        self.cli("set nat timeout udp 300")
        self.cli("natgw offload enable")
        wait_for(lambda: self.offloaded() == 0 and self.offload()["pending sessions"] == 0,
                 what="offloaded sessions cleared")
        self.cli("clear natgw offload counters")


@pytest.fixture(scope="module")
def gw():
    reason = vppenv.skip_reason()
    if reason:
        pytest.skip(reason)
    g = Gateway("a")
    yield g
    g.close()


@pytest.fixture
def fresh(gw):
    gw.reset()
    yield gw
    gw.reset()
