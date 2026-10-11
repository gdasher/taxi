# SPDX-License-Identifier: BSD-3-Clause
"""Test doubles: raw stats snapshots, a WAN manager status, a clock."""

import copy

from natgw_monitor.sources import SourceDown

PORTS = ["NatgwModel0", "NatgwModel1", "NatgwModel2"]
REASONS = ["miss", "bypass", "not_ipv4", "mcast", "ip_hdr", "frag", "ttl", "proto", "csum", "syn", "finrst",
           "vlan", "nh", "rsvd13", "rsvd14"]


class Clock:
    def __init__(self, t=1.7e9):
        self.t = t

    def __call__(self):
        return self.t


def xstats():
    x = {"rx_forwarded": 0, "punt_ingress_drop": 0, "punt_bad_fcs": 0}
    x.update({f"punt_{r}": 0 for r in REASONS})
    x.update(flows=0, events_fin=0, events_rst=0, events_lost=0, ddr_flows=0, ddr_lookups=0, ddr_hits=0,
             ddr_skips=0, ddr_read_errors=0, promotions=0, demotions=0, migrate_failures=0, onchip_load_pct=0,
             ddr_skips_last_sweep=0, onchip_capacity=1000, ddr_capacity=4000, ddr_status=0x17)
    return x


OFFLOAD_KEYS = ["events", "installed", "removed", "reinstalled", "skip_proto", "skip_flags", "skip_ops",
                "skip_no_route", "skip_not_natgw", "skip_vlan", "skip_flow_error", "table_full", "retry_gave_up",
                "queue_overflow", "synced", "expired", "aged_events", "stale", "bulk"]


class FakeStats:
    """a StatsSource stand-in: read() returns the raw snapshot it holds"""

    def __init__(self):
        names = ["local0"] + PORTS
        self.down = None
        self.raw = {
            "boottime": 1000.0, "heartbeat": 1,
            "if_names": names,
            "if_rx": [(0, 0)] * len(names), "if_tx": [(0, 0)] * len(names),
            "if_drops": [0] * len(names), "if_rx_miss": [0] * len(names),
            "xstats": {p: xstats() for p in PORTS},
            "offload": dict({k: 0 for k in OFFLOAD_KEYS}, enabled=1, pending=0, offloaded=0),
            "link": {p: 3 for p in PORTS},
            "nat": {"total-sessions": 0, "max-cfg-sessions": 1000, "in2out/fastpath/udp": 0,
                    "in2out/slowpath/udp": 0, "out2in/fastpath/drops": 0},
            "sys": {"vector_rate": 1.0, "input_rate": 0.0, "num_worker_threads": 0},
        }

    def read(self):
        if self.down:
            raise SourceDown(self.down)
        return copy.deepcopy(self.raw)

    def port(self, p):
        return self.raw["xstats"][p]

    def bump(self, p, **kw):
        """add to a port's xstats (device-wide ones go to every port)"""
        for k, v in kw.items():
            for q in (PORTS if k not in ("rx_forwarded", "punt_bad_fcs", "punt_ingress_drop")
                      and not k.startswith("punt_") else [p]):
                self.raw["xstats"][q][k] += v

    def set_dev(self, **kw):
        for q in PORTS:
            self.raw["xstats"][q].update(kw)

    def if_add(self, name, rx=0, tx=0):
        i = self.raw["if_names"].index(name)
        p, b = self.raw["if_rx"][i]
        self.raw["if_rx"][i] = (p + rx, b + 100 * rx)
        p, b = self.raw["if_tx"][i]
        self.raw["if_tx"][i] = (p + tx, b + 100 * tx)


def wan(name, interface, state="up", since=None, reason="probe target 192.0.2.1 answering", lease=True):
    return {"state": state, "since": since, "reason": reason, "interface": interface, "weight": 1,
            "in_route": state == "up",
            "link": {"admin_up": True, "link_up": True},
            "lease": {"address": f"198.51.100.{interface[-1]}", "prefix_len": 24, "gateway": "198.51.100.254"}
            if lease else None,
            "lease_since": since, "good_rounds": 5, "bad_rounds": 0, "history": "11111",
            "probe_targets": {"192.0.2.1": {"ok": state == "up", "last_ok": since, "last_fail": None}},
            "last_probe": "192.0.2.1"}


class FakeWans:
    def __init__(self, clock):
        self.clock = clock
        self.down = None
        self.st = {"updated": clock(), "connected": True, "rounds": 1,
                   "probe": {"interval": 2.0, "timeout": 0.5, "up_rounds": 5, "down_rounds": 3},
                   "route": [{"sw_if_index": 2, "gateway": "a", "weight": 1},
                             {"sw_if_index": 3, "gateway": "b", "weight": 1}],
                   "wans": {"wan1": wan("wan1", "NatgwModel1", since=clock() - 100),
                            "wan2": wan("wan2", "NatgwModel2", since=clock() - 100)},
                   "events": []}

    def read(self):
        if self.down:
            raise SourceDown(self.down)
        st = copy.deepcopy(self.st)
        return st

    def tick(self):
        self.st["updated"] = self.clock()

    def set_state(self, name, state, reason, event=True):
        w = self.st["wans"][name]
        prev = w["state"]
        w.update(state=state, since=self.clock(), reason=reason, in_route=state == "up")
        w["probe_targets"]["192.0.2.1"]["ok"] = state == "up"
        if event:
            self.st["events"].append({"ts": self.clock(), "wan": name, "kind": "state", "from": prev, "to": state,
                                      "detail": reason})
