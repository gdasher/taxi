# SPDX-License-Identifier: BSD-3-Clause
"""The monitor on synthetic data, for working on the page (and the page
tests): a week of history (a daily traffic cycle, three past ISP outages, a
VPP restart) and live samples with wan2 down.

    python3 tests/preview.py [--port 8443] [--state DIR]     # then open http://127.0.0.1:8443/
"""

import argparse
import math
import os
import random
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from monfakes import FakeStats, FakeWans, wan  # noqa: E402
from natgw_monitor import config  # noqa: E402
from natgw_monitor.collector import PUNT_REASONS, Collector  # noqa: E402
from natgw_monitor.store import Store  # noqa: E402
from natgw_monitor.web import Web  # noqa: E402

WEEK = 7 * 86400


def load(t, rnd):
    """packets/s offered at time t: a daily cycle with noise"""
    day = (t % 86400) / 86400
    base = 60000 + 140000 * max(0.0, math.sin(math.pi * (day - 0.25) / 0.75)) ** 1.5
    return base * (0.9 + 0.2 * rnd.random())


PAST_OUTAGES = [  # (seconds before now, length, wan, reason)
    (5.5 * 86400, 1260, "wan1", "no reply from probe targets (1.1.1.1, 9.9.9.9)"),
    (3.2 * 86400, 410, "wan2", "link down"),
    (20 * 3600, 95, "wan2", "no DHCP lease"),
]
VPP_RESTART = 2.6 * 86400


def series_at(t, now, rnd):
    off = load(t, rnd)
    punt = off * (0.035 + 0.02 * rnd.random())
    hw = off - punt
    out = {"hw": hw, "punt": punt, "drop": rnd.random() * 2, "ratio": 100 * hw / off,
           "onchip": 55 + 30 * (off / 220000), "ddr": 8 + 20 * (off / 220000),
           "onchip_flows": 140000 + 80000 * (off / 220000), "ddr_flows": 90000 + 200000 * (off / 220000),
           "prom": rnd.random() * 12, "dem": rnd.random() * 14, "hit": 92 + 6 * rnd.random(), "vr": 1.5 + rnd.random(),
           "vpp": 1}
    shares = {"miss": .55, "syn": .2, "finrst": .12, "frag": .03, "ttl": .02, "not_ipv4": .04, "mcast": .02,
              "ip_hdr": .01, "proto": .01}
    for k in PUNT_REASONS:
        out[f"r.{k}"] = punt * shares.get(k, 0) * (0.8 + 0.4 * rnd.random())
    for name, share in (("wan1", 0.58), ("wan2", 0.42)):
        down = any(now - ago <= t < now - ago + length and w == name for ago, length, w, _ in PAST_OUTAGES)
        out[f"w.{name}.up"] = 0 if down else 1
        out[f"w.{name}.rx"] = 0.0 if down else hw * share * 0.55
    if now - VPP_RESTART <= t < now - VPP_RESTART + 40:
        out = dict.fromkeys(out, None)
        out["vpp"] = 0
    return out


def seed(state_dir, now):
    rnd = random.Random(7)
    st = Store(state_dir, clock=lambda: now)
    roll, raw = [], []
    for m in range(int(now - WEEK) // 60 * 60, int(now - 86400), 60):
        s = series_at(m, now, rnd)
        row = {}
        for k, v in s.items():
            if v is None:
                continue
            row[f"{k}|min"], row[f"{k}|avg"], row[f"{k}|max"] = v * 0.8, v, v * (1.25 if k in ("hw", "punt") else 1.0)
        roll.append((m, row))
    for t in range(int(now - 86400), int(now) - 5):
        raw.append((t, series_at(t, now, rnd)))
    # the last day's minute summaries from its seconds
    acc = {}
    for t, s in raw:
        m = t // 60 * 60
        a = acc.setdefault(m, {})
        for k, v in s.items():
            if v is None:
                continue
            lo, hi, sm, n = a.get(k, (v, v, 0.0, 0))
            a[k] = (min(lo, v), max(hi, v), sm + v, n + 1)
    for m, a in acc.items():
        roll.append((m, {f"{k}|{f}": x for k, (lo, hi, sm, n) in a.items()
                         for f, x in (("min", lo), ("avg", sm / n), ("max", hi))}))
    cnt = {"port.NatgwModel1.rx_forwarded": 0}
    counters = []
    for m, row in roll:
        cnt["port.NatgwModel1.rx_forwarded"] += int((row.get("hw|avg") or 0) * 0.3 * 60)
        counters.append((m, dict(cnt)))
    with st.lock:
        for table, rows in (("rollup", roll), ("counters", counters), ("samples", raw)):
            st._ensure(table, sorted({k for _, r in rows for k in r}))
        st.d.execute("BEGIN")
        for i in range(0, len(raw), 5000):      # literal VALUES: keep the statements modest
            st._insert("samples", raw[i:i + 5000])
        st._insert("rollup", roll)
        st._insert("counters", counters)
        st.d.execute("COMMIT")
    for ago, length, w, reason in PAST_OUTAGES:
        t0 = now - ago
        st.add_event({"type": "wan", "ts": t0, "wan": w, "kind": "state", "from": "up", "to": "down", "detail": reason})
        st.add_event({"type": "outage_start", "ts": t0, "wan": w, "reason": reason})
        st.outage_open(w, t0, reason, {"reason": reason, "link": {"admin_up": True, "link_up": reason != "link down"},
                                       "lease": None if "lease" in reason else
                                       {"address": "198.51.100.127", "prefix_len": 24, "gateway": "198.51.100.1"},
                                       "probe_targets": {"1.1.1.1": {"ok": False}, "9.9.9.9": {"ok": False}},
                                       "others": {"wan1" if w == "wan2" else "wan2": "up"}, "rx_pps_before": 61000})
        st.add_event({"type": "outage_end", "ts": t0 + length, "wan": w, "start": t0, "duration": length,
                      "reason": reason})
        st.add_event({"type": "wan", "ts": t0 + length, "wan": w, "kind": "state", "from": "down", "to": "up",
                      "detail": "probe target answering"})
        st.outage_close(w, t0, t0 + length)
    st.add_event({"type": "vpp", "ts": now - VPP_RESTART, "to": "restarted", "detail": ""})
    st.close()


class LiveStats(FakeStats):
    """counters that grow like the synthetic week, with the DDR tier on"""

    def __init__(self):
        super().__init__()
        self.rnd = random.Random(3)
        self.set_dev(onchip_capacity=262144, ddr_capacity=4194304, ddr_status=0x17, flows=231000, ddr_flows=102000,
                     onchip_load_pct=49)
        self.raw["nat"]["total-sessions"] = 212000
        self.raw["nat"]["max-cfg-sessions"] = 262144
        self.raw["offload"]["offloaded"] = 205000
        self.raw["offload"]["pending"] = 3

    def read(self):
        t = time.time()
        off = load(t, self.rnd)
        punt = off * 0.04
        lanes = {"NatgwModel0": 0.5, "NatgwModel1": 0.5, "NatgwModel2": 0.0}    # wan2 is down
        for p, share in lanes.items():
            self.bump(p, rx_forwarded=int((off - punt) * share), punt_miss=int(punt * share * .6),
                      punt_syn=int(punt * share * .25), punt_finrst=int(punt * share * .15))
        self.bump("NatgwModel0", ddr_lookups=40000, ddr_hits=38000, promotions=self.rnd.randrange(8),
                  demotions=self.rnd.randrange(9), events_fin=900, events_rst=80)
        self.if_add("NatgwModel1", rx=int(punt * .3), tx=int(punt * .5))
        self.if_add("NatgwModel2", rx=0, tx=2)
        return super().read()


class LiveWans(FakeWans):
    def __init__(self, clock, wan2_down_for=252):
        super().__init__(clock)
        now = clock()
        self.st["wans"]["wan1"] = wan("wan1", "NatgwModel1", since=now - 20 * 3600 + 95)
        self.st["wans"]["wan2"] = wan("wan2", "NatgwModel2", state="down", since=now - wan2_down_for,
                                      reason="no reply from probe targets (1.0.0.1, 149.112.112.112)")
        w1, w2 = self.st["wans"]["wan1"], self.st["wans"]["wan2"]
        w1["lease"] = {"address": "198.51.100.127", "prefix_len": 24, "gateway": "198.51.100.1"}
        w1["lease_since"] = now - 3 * 86400
        w1["history"] = "1" * 60
        w1["probe_targets"] = {"1.1.1.1": {"ok": True, "last_ok": now - 1, "last_fail": now - 20 * 3600},
                               "9.9.9.9": {"ok": None, "last_ok": now - 20 * 3600, "last_fail": None}}
        w2["lease"] = {"address": "203.0.113.54", "prefix_len": 22, "gateway": "203.0.113.1"}
        w2["lease_since"] = now - 5 * 86400
        w2["history"] = "1" * 33 + "0" * 27
        w2["probe_targets"] = {"1.0.0.1": {"ok": False, "last_ok": now - wan2_down_for - 6, "last_fail": now - 1},
                               "149.112.112.112": {"ok": False, "last_ok": now - wan2_down_for - 6,
                                                   "last_fail": now - 1}}
        w2["bad_rounds"] = 27
        w2["good_rounds"] = 0
        self.st["route"] = [{"sw_if_index": 2, "gateway": "198.51.100.1", "weight": 1}]
        self.st["events"] = [{"ts": now - wan2_down_for, "wan": "wan2", "kind": "state", "from": "up", "to": "down",
                              "detail": w2["reason"]}]

    def read(self):
        self.tick()
        return super().read()


def serve(port=0, state_dir=None, stop=None, tls=False, cert=None, key=None):
    """start a preview monitor; returns (web, collector, stop event)"""
    state_dir = state_dir or tempfile.mkdtemp(prefix="natgw-preview-")
    now = time.time()
    if not os.path.exists(os.path.join(state_dir, "stats.duckdb")):
        seed(state_dir, now)
    cfg = config.parse(f"""
[http]
bind = ["127.0.0.1"]
port = {port or 1}
[tls]
enabled = {"true" if tls else "false"}
{f'cert = "{cert}"' if cert else ''}
{f'key = "{key}"' if key else ''}
[store]
state_dir = "{state_dir}"
""")
    cfg.http.port = port
    stop = stop or threading.Event()
    col = Collector(cfg, LiveStats(), LiveWans(time.time), clock=time.time)
    # an hour of live-looking samples in memory, so the short windows are full
    threading.Thread(target=col.run, args=(stop,), daemon=True).start()
    web = Web(cfg, col, hostname="natgw-site1")
    web.start(stop)
    return web, col, stop


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--state")
    a = ap.parse_args()
    web, col, stop = serve(a.port, a.state)
    print(f"http://127.0.0.1:{web.addresses()[0][1]}/  (state in {col.store.path})", flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
