# SPDX-License-Identifier: BSD-3-Clause
"""Sampling: raw snapshots -> the current view (with rates), history for the
graphs, alerts, and the persistent event log with ISP outages.

Every sample_interval the collector reads both sources and builds `current`
(served as /api/stats). Rates are per second over the last interval; a counter
that goes backwards (VPP restart, counters cleared) gives no rate for that
interval, except the device's 32-bit DDR counters, which wrap.

ISP outages: when a WAN is DOWN the collector opens an outage record with a
snapshot of the evidence (reason, lease, link, probe results, the other WANs,
the route, the WAN's traffic just before), and closes it when the WAN is UP
again. Records go to <state_dir>/events.jsonl with the WAN manager's state
and lease events and alert changes, so the history survives restarts of the
monitor; the WAN manager's own event log (its last 200 events) fills in what
happened while the monitor was not running."""

import collections
import json
import logging
import os
import threading
import time

from .sources import SourceDown

log = logging.getLogger("natgw-monitor")

PUNT_REASONS = ["miss", "bypass", "not_ipv4", "mcast", "ip_hdr", "frag", "ttl", "proto", "csum", "syn",
                "finrst", "vlan", "nh"]
DROPS = ["ingress_drop", "bad_fcs"]
WRAP32 = {"ddr_lookups", "ddr_hits", "ddr_skips", "ddr_read_errors"}
DDR_BITS = ["present", "calibrated", "enabled", "clearing", "active"]   # NATGW_DDR_* bit order

# counters whose every rise is worth an alert: (id, where, key, severity, title)
RISE_ALERTS = [
    ("bad_fcs", "port", "punt_bad_fcs", "warning", "Frames with bad FCS received"),
    ("ddr_read_errors", "device", "ddr_read_errors", "critical", "DDR read errors"),
    ("events_lost", "device", "events_lost", "warning", "Device events lost (host missed idle/FIN/RST)"),
    ("migrate_failures", "device", "migrate_failures", "warning", "Tier moves failed"),
    ("table_full", "offload", "table_full", "warning", "Flow table full: sessions not offloaded"),
    ("queue_overflow", "offload", "queue_overflow", "warning", "Offload install queue overflowed"),
    ("retry_gave_up", "offload", "retry_gave_up", "warning", "Offload installs gave up after retries"),
    ("flow_error", "offload", "skip_flow_error", "warning", "rte_flow rejected offloads"),
]

OUTAGE_LOOKBACK = 60.0   # seconds of traffic before an outage kept in its snapshot


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else None


class Rates:
    """per-second rates of monotonically increasing counters"""

    def __init__(self):
        self.prev = {}

    def reset(self):
        self.prev = {}

    def rate(self, key, value, now, wrap32=False):
        if value is None:
            return None
        p = self.prev.get(key)
        self.prev[key] = (value, now)
        if p is None or now <= p[1]:
            return None
        d = value - p[0]
        if d < 0:
            if not wrap32:
                return None
            d += 1 << 32
        return d / (now - p[1])


class EventLog:
    """append-only JSON lines, most recent kept in memory"""

    MAX_BYTES = 8 << 20
    KEEP_LINES = 20000

    def __init__(self, state_dir, keep=1000):
        self.path = os.path.join(state_dir, "events.jsonl") if state_dir else None
        self.recent = collections.deque(maxlen=keep)
        self.lock = threading.Lock()
        self.loaded = []
        if self.path:
            os.makedirs(state_dir, exist_ok=True)
            self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return
        if sum(len(x) for x in lines) > self.MAX_BYTES:
            lines = lines[-self.KEEP_LINES:]
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.replace(tmp, self.path)
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue    # a torn last line after a crash
            self.loaded.append(r)
            self.recent.append(r)

    def add(self, rec):
        with self.lock:
            self.recent.append(rec)
            if self.path:
                with open(self.path, "a") as f:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def tail(self, n):
        with self.lock:
            return list(self.recent)[-n:]


class Alerts:
    """active alerts with the time they were raised; changes are logged"""

    def __init__(self, events, clock):
        self.events = events
        self.clock = clock
        self.active = {}       # id -> alert dict

    def update(self, found):
        now = self.clock()
        seen = set()
        for a in found:
            seen.add(a["id"])
            cur = self.active.get(a["id"])
            if cur is None:
                a = dict(a, since=now)
                self.active[a["id"]] = a
                self.events.add({"type": "alert", "ts": now, "state": "raised", **{k: a[k] for k in
                                 ("id", "severity", "title", "detail")}})
            else:
                cur.update(severity=a["severity"], title=a["title"], detail=a["detail"])
        for aid in list(self.active):
            if aid not in seen:
                a = self.active.pop(aid)
                self.events.add({"type": "alert", "ts": now, "state": "cleared", "id": aid,
                                 "severity": a["severity"], "title": a["title"],
                                 "detail": f"after {now - a['since']:.0f} s"})

    def list(self):
        order = {"critical": 0, "warning": 1, "info": 2}
        return sorted(self.active.values(), key=lambda a: (order.get(a["severity"], 3), a["since"]))


class Collector:
    def __init__(self, cfg, stats, wans, clock=time.time):
        self.cfg = cfg
        self.stats = stats
        self.wans = wans
        self.clock = clock
        self.rates = Rates()
        self.events = EventLog(cfg.store.state_dir)
        self.alerts = Alerts(self.events, clock)
        n = int(cfg.store.history_seconds / cfg.alerts.sample_interval) + 1
        self.history = collections.deque(maxlen=n)
        self.lock = threading.Lock()
        self.current = None
        self.boottime = None
        self.vpp_up = None
        self.rises = {}             # alert id -> deque[(ts, delta)]
        self.rise_prev = {}
        self.fill_since = None
        self.seen_wan_events = set()
        self.outages = {}           # wan -> open outage record
        self.closed = collections.deque(maxlen=200)
        self._restore()
        self.events.add({"type": "monitor", "ts": clock(), "detail": "started"})

    # ------------------------------------------------------------ persistence

    def _restore(self):
        for r in self.events.loaded:
            t = r.get("type")
            if t == "wan":
                self.seen_wan_events.add(self._wan_event_key(r))
            elif t == "outage_start":
                self.outages[r["wan"]] = r
            elif t == "outage_end":
                o = self.outages.pop(r["wan"], None)
                self.closed.append(self._closed(o, r) if o else r)
        self.events.loaded = []

    @staticmethod
    def _wan_event_key(e):
        return (e.get("ts"), e.get("wan"), e.get("kind"), e.get("to"))

    @staticmethod
    def _closed(start, end):
        return {"wan": start["wan"], "start": start["ts"], "end": end["ts"],
                "duration": end["ts"] - start["ts"], "reason": start.get("reason"),
                "snapshot": start.get("snapshot")}

    # ------------------------------------------------------------ sampling

    def run(self, stop):
        nxt = time.monotonic()
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("sample failed")
            nxt += self.cfg.alerts.sample_interval
            d = nxt - time.monotonic()
            if d < 0:
                nxt = time.monotonic()
            else:
                stop.wait(d)

    def step(self):
        now = self.clock()
        cur = {"ts": now}
        try:
            raw = self.stats.read()
            err = None
        except SourceDown as e:
            raw, err = None, str(e)
        self._vpp(cur, raw, err, now)
        try:
            ws = self.wans.read()
            werr = None
        except SourceDown as e:
            ws, werr = None, str(e)
        self._wan(cur, ws, werr, raw, now)
        self._outages(cur, now)
        self._wan_events(ws)
        self.alerts.update(self._find_alerts(cur, now))
        cur["alerts"] = self.alerts.list()
        cur["outages"] = self.outage_list()
        with self.lock:
            self.current = cur
            self.history.append(self._compact(cur))
        return cur

    # ------------------------------------------------------------ VPP side

    def _vpp(self, cur, raw, err, now):
        up = raw is not None
        if up != self.vpp_up:
            if self.vpp_up is not None or not up:
                self.events.add({"type": "vpp", "ts": now, "to": "up" if up else "down", "detail": err or ""})
            self.vpp_up = up
        cur["vpp"] = {"up": up, "error": err}
        if not up:
            self.rates.reset()
            return
        if raw["boottime"] != self.boottime:
            if self.boottime is not None:
                self.events.add({"type": "vpp", "ts": now, "to": "restarted", "detail": ""})
            self.boottime = raw["boottime"]
            self.rates.reset()
            self.rise_prev = {}
        r = self.rates.rate
        sysv = raw["sys"]
        cur["vpp"].update(boottime=raw["boottime"], vector_rate=sysv.get("vector_rate"),
                          input_rate=sysv.get("input_rate"), workers=sysv.get("num_worker_threads"))

        off = raw["offload"]
        counters = {k: v for k, v in off.items() if k not in ("enabled", "pending", "offloaded")}
        cur["offload"] = {"enabled": bool(off.get("enabled")), "offloaded": off.get("offloaded"),
                          "pending": off.get("pending"), "counters": counters,
                          "rates": {k: r(f"off.{k}", v, now) for k, v in counters.items()}}

        names = raw["if_names"]
        ports = []
        hw = punt = drops = 0
        reasons = dict.fromkeys(PUNT_REASONS, 0.0)
        have_rates = True
        for name in sorted(raw["xstats"]):
            x = raw["xstats"][name]
            idx = names.index(name) if name in names else None
            rates = {k: r(f"x.{name}.{k}", v, now, k in WRAP32) for k, v in x.items()}
            sw = self._if_counters(raw, idx)
            swr = {k: r(f"if.{name}.{k}", v, now) for k, v in sw.items()}
            link = raw["link"].get(name)
            p = {"name": name, "sw_if_index": idx, "natgw": x, "rates": rates, "vpp": sw, "vpp_rates": swr,
                 "link": None if link is None else {"admin_up": bool(link & 1), "link_up": bool(link & 2)}}
            ports.append(p)
            if rates.get("rx_forwarded") is None:
                have_rates = False
                continue
            hw += rates["rx_forwarded"]
            for k in PUNT_REASONS:
                v = rates.get(f"punt_{k}") or 0.0
                reasons[k] += v
                punt += v
            drops += sum(rates.get(f"punt_{k}") or 0.0 for k in DROPS)
        cur["ports"] = ports
        if ports and have_rates:
            total = hw + punt
            cur["traffic"] = {"hw_pps": hw, "punt_pps": punt, "drop_pps": drops,
                              "offload_pct": _pct(hw, total) if total else None, "punt_reasons": reasons}
        else:
            cur["traffic"] = {"hw_pps": None, "punt_pps": None, "drop_pps": None, "offload_pct": None,
                              "punt_reasons": {}}

        dev = {}
        if ports:
            x, xr = ports[0]["natgw"], ports[0]["rates"]
            flows, ddr = x.get("flows", 0), x.get("ddr_flows", 0)
            st = x.get("ddr_status", 0)
            dev = {
                "flows": flows,
                "onchip_flows": flows - ddr,
                "onchip_capacity": x.get("onchip_capacity"),
                "onchip_pct": _pct(flows - ddr, x.get("onchip_capacity")),
                "onchip_load_pct": x.get("onchip_load_pct"),
                "ddr_flows": ddr,
                "ddr_capacity": x.get("ddr_capacity"),
                "ddr_pct": _pct(ddr, x.get("ddr_capacity")),
                "ddr_status": {b: bool(st & (1 << i)) for i, b in enumerate(DDR_BITS)},
                "ddr_in_use": bool(x.get("ddr_capacity")),
                "ddr_lookups_ps": xr.get("ddr_lookups"),
                "ddr_hits_ps": xr.get("ddr_hits"),
                "ddr_hit_pct": _pct(xr.get("ddr_hits") or 0, xr.get("ddr_lookups")) if xr.get("ddr_lookups")
                else None,
                "ddr_skips_ps": xr.get("ddr_skips"),
                "ddr_skips_last_sweep": x.get("ddr_skips_last_sweep"),
                "ddr_read_errors": x.get("ddr_read_errors"),
                "promotions_ps": xr.get("promotions"),
                "demotions_ps": xr.get("demotions"),
                "promotions": x.get("promotions"),
                "demotions": x.get("demotions"),
                "migrate_failures": x.get("migrate_failures"),
                "events_fin_ps": xr.get("events_fin"),
                "events_rst_ps": xr.get("events_rst"),
                "events_lost": x.get("events_lost"),
            }
        cur["device"] = dev

        nat = raw["nat"]
        fast = sum(v for k, v in nat.items() if "/fastpath/" in k and not k.endswith("drops"))
        slow = sum(v for k, v in nat.items() if "/slowpath/" in k and not k.endswith("drops"))
        ndrop = sum(v for k, v in nat.items() if k.endswith("/drops"))
        cur["nat"] = {"sessions": nat.get("total-sessions"), "max_sessions": nat.get("max-cfg-sessions"),
                      "sessions_pct": _pct(nat.get("total-sessions") or 0, nat.get("max-cfg-sessions")),
                      "fastpath_pps": r("nat.fast", fast, now), "slowpath_pps": r("nat.slow", slow, now),
                      "drops_pps": r("nat.drops", ndrop, now)}

    @staticmethod
    def _if_counters(raw, idx):
        if idx is None:
            return {}

        def at(v, default):
            return v[idx] if idx < len(v) else default
        rx = at(raw["if_rx"], (0, 0))
        tx = at(raw["if_tx"], (0, 0))
        return {"rx_packets": rx[0], "rx_bytes": rx[1], "tx_packets": tx[0], "tx_bytes": tx[1],
                "drops": at(raw["if_drops"], 0), "rx_miss": at(raw["if_rx_miss"], 0)}

    # ------------------------------------------------------------ ISPs

    def _wan(self, cur, ws, err, raw, now):
        stale = self.cfg.alerts.wanmgr_stale_seconds
        mgr = {"up": False, "error": err, "updated": None, "connected": None, "route": [], "probe": None}
        wans = []
        if ws is not None:
            upd = ws.get("updated")
            age = now - upd if upd else None
            mgr.update(updated=upd, connected=ws.get("connected"), route=ws.get("route", []),
                       probe=ws.get("probe"), age=age)
            if upd is None:
                mgr["error"] = "status without timestamps (old WAN manager?)"
            elif age > stale:
                mgr["error"] = f"status not updated for {age:.0f} s (WAN manager stopped?)"
            elif not ws.get("connected"):
                mgr["error"] = "WAN manager not connected to VPP"
            else:
                mgr["up"] = True
            names = raw["if_names"] if raw else []
            ports = {p["name"]: p for p in cur.get("ports", [])}
            per_port = collections.Counter(w.get("interface", "").split(".")[0] for w in ws["wans"].values())
            for name, w in ws["wans"].items():
                w = dict(w, name=name)
                ifname = w.get("interface") or ""
                parent = ifname.split(".")[0]
                idx = names.index(ifname) if ifname in names else None
                r = self.rates.rate
                sw = self._if_counters(raw, idx) if raw else {}
                sw_rx = r(f"wan.{name}.rx", sw.get("rx_packets"), now)
                sw_tx = r(f"wan.{name}.tx", sw.get("tx_packets"), now)
                p = ports.get(parent)
                hw = p["rates"].get("rx_forwarded") if p and per_port[parent] == 1 else None
                # inbound from the ISP: frames the FPGA forwarded on the WAN's
                # lane (when the lane is this WAN's alone) plus VPP's; outbound
                # is VPP's only (the FPGA counts per ingress lane)
                w["traffic"] = {"port": parent if p else None, "sw_rx_pps": sw_rx, "sw_tx_pps": sw_tx,
                                "hw_rx_pps": hw,
                                "rx_pps": None if sw_rx is None else sw_rx + (hw or 0)}
                if p and p["link"] and w.get("link") is None:
                    w["link"] = p["link"]
                if not mgr["up"]:
                    w["stale"] = True
                wans.append(w)
        cur["wanmgr"] = mgr
        cur["wans"] = wans

    def _wan_events(self, ws):
        if ws is None:
            return
        for e in ws.get("events", []):
            if e.get("kind") not in ("state", "lease"):
                continue
            k = self._wan_event_key(e)
            if k in self.seen_wan_events:
                continue
            self.seen_wan_events.add(k)
            self.events.add({"type": "wan", **e})

    def _outages(self, cur, now):
        if not cur["wanmgr"]["up"]:
            return      # no verdicts from a stopped manager: leave outages as they are
        for w in cur["wans"]:
            name, state = w["name"], w.get("state")
            o = self.outages.get(name)
            if state == "down" and o is None:
                start = w.get("since") or now
                rec = {"type": "outage_start", "ts": start, "wan": name, "reason": w.get("reason"),
                       "snapshot": self._snapshot(cur, w, start)}
                self.outages[name] = rec
                self.events.add(rec)
            elif state == "up" and o is not None:
                end = w.get("since") or now
                if end < o["ts"]:
                    end = now
                rec = {"type": "outage_end", "ts": end, "wan": name, "start": o["ts"],
                       "duration": end - o["ts"], "reason": o.get("reason")}
                self.events.add(rec)
                self.closed.append(self._closed(o, rec))
                del self.outages[name]
        for w in cur["wans"]:
            o = self.outages.get(w["name"])
            w["outage"] = None if o is None else {"start": o["ts"], "duration": now - o["ts"],
                                                  "reason": o.get("reason")}

    def _snapshot(self, cur, w, start):
        """the evidence at the start of an outage"""
        before = [h for h in list(self.history) if start - OUTAGE_LOOKBACK <= h["t"] < start]
        rx = [h["wans"].get(w["name"], {}).get("rx") for h in before]
        rx = [v for v in rx if v is not None]
        return {
            "taken": cur["ts"],
            "interface": w.get("interface"),
            "reason": w.get("reason"),
            "link": w.get("link"),
            "lease": w.get("lease"),
            "probe_targets": w.get("probe_targets"),
            "history": w.get("history"),
            "bad_rounds": w.get("bad_rounds"),
            "in_route": w.get("in_route"),
            "others": {o["name"]: o.get("state") for o in cur["wans"] if o["name"] != w["name"]},
            "route": cur["wanmgr"].get("route"),
            "rx_pps_before": round(sum(rx) / len(rx), 1) if rx else None,
            "offloaded": cur.get("offload", {}).get("offloaded"),
            "nat_sessions": cur.get("nat", {}).get("sessions"),
        }

    def outage_list(self):
        now = self.clock()
        cur = [{"wan": o["wan"], "start": o["ts"], "end": None, "duration": now - o["ts"],
                "reason": o.get("reason"), "snapshot": o.get("snapshot")} for o in self.outages.values()]
        return sorted(cur, key=lambda o: -o["start"]) + list(reversed(self.closed))[:50]

    # ------------------------------------------------------------ alerts

    def _rise(self, aid, key, value, now):
        """True while `value` rose within hold_seconds; returns the rise"""
        if value is None:
            return 0
        q = self.rises.setdefault(aid, collections.deque())
        prev = self.rise_prev.get(key)
        self.rise_prev[key] = value
        if prev is not None and value > prev:
            q.append((now, value - prev))
        while q and now - q[0][0] > self.cfg.alerts.hold_seconds:
            q.popleft()
        return sum(d for _, d in q)

    def _find_alerts(self, cur, now):
        a = []
        cfg = self.cfg.alerts

        def add(aid, sev, title, detail=""):
            a.append({"id": aid, "severity": sev, "title": title, "detail": detail})

        vpp = cur["vpp"]
        if not vpp["up"]:
            add("vpp_down", "critical", "VPP is not running or not reachable", vpp["error"] or "")
        mgr = cur["wanmgr"]
        if not mgr["up"]:
            add("wanmgr", "warning", "WAN manager not reporting: ISP state unknown", mgr["error"] or "")
        wans = cur["wans"]
        if mgr["up"] and wans:
            up = [w["name"] for w in wans if w.get("state") == "up"]
            for w in wans:
                if w.get("state") == "down":
                    others = f"; traffic on {', '.join(up)}" if up else ""
                    add(f"wan_down:{w['name']}", "critical" if not up else "warning",
                        f"{w['name']} ({w.get('interface')}) is DOWN", f"{w.get('reason') or ''}{others}")
            if not up and any(w.get("state") == "down" for w in wans):
                add("all_wans_down", "critical", "No ISP is working",
                    "every WAN is down: falling back to the DHCP routes of all WANs")
        if vpp["up"]:
            for p in cur["ports"]:
                ln = p["link"]
                if ln and ln["admin_up"] and not ln["link_up"]:
                    add(f"link:{p['name']}", "critical", f"{p['name']}: link down")
            off = cur["offload"]
            if not off["enabled"]:
                add("offload_disabled", "warning", "FPGA offload is disabled", "all traffic takes VPP's path")
            dev = cur["device"]
            st = dev.get("ddr_status") or {}
            if st.get("present") and not st.get("calibrated"):
                add("ddr_uncalibrated", "warning", "DDR tier present but not calibrated",
                    "no DIMM, or the memory failed calibration: on-chip table only")
            pct = dev.get("onchip_pct")
            if pct is not None and pct > cfg.onchip_fill_pct:
                self.fill_since = self.fill_since or now
                if now - self.fill_since >= cfg.onchip_fill_seconds:
                    add("onchip_fill", "warning", f"On-chip flow table {pct:.0f}% full",
                        f"above {cfg.onchip_fill_pct:.0f}% for {now - self.fill_since:.0f} s")
            else:
                self.fill_since = None
            ns = cur["nat"].get("sessions_pct")
            if ns is not None and ns > 90:
                add("nat_sessions", "warning", f"NAT sessions {ns:.0f}% of the maximum")
            for aid, where, key, sev, title in RISE_ALERTS:
                if where == "port":
                    for p in cur["ports"]:
                        n = self._rise(f"{aid}:{p['name']}", f"{aid}:{p['name']}", p["natgw"].get(key), now)
                        if n:
                            add(f"{aid}:{p['name']}", sev, f"{title} on {p['name']}",
                                f"{n} in the last {cfg.hold_seconds:.0f} s")
                    continue
                v = cur["device"].get(key) if where == "device" else cur["offload"]["counters"].get(key)
                n = self._rise(aid, aid, v, now)
                if n:
                    add(aid, sev, title, f"{n} in the last {cfg.hold_seconds:.0f} s")
        return a

    # ------------------------------------------------------------ history

    @staticmethod
    def _compact(cur):
        t = cur.get("traffic", {})
        d = cur.get("device", {})
        return {
            "t": cur["ts"],
            "vpp": cur["vpp"]["up"],
            "hw": t.get("hw_pps"), "punt": t.get("punt_pps"), "drop": t.get("drop_pps"),
            "ratio": t.get("offload_pct"),
            "reasons": {k: v for k, v in (t.get("punt_reasons") or {}).items() if v},
            "onchip": d.get("onchip_pct"), "ddr": d.get("ddr_pct"),
            "onchip_flows": d.get("onchip_flows"), "ddr_flows": d.get("ddr_flows"),
            "prom": d.get("promotions_ps"), "dem": d.get("demotions_ps"), "hit": d.get("ddr_hit_pct"),
            "vr": cur["vpp"].get("vector_rate"),
            "wans": {w["name"]: {"up": {"up": 1, "down": 0}.get(w.get("state")),
                                 "rx": (w.get("traffic") or {}).get("rx_pps")} for w in cur.get("wans", [])},
        }

    def history_since(self, seconds, max_points=600):
        with self.lock:
            h = list(self.history)
        if not h:
            return []
        t0 = h[-1]["t"] - seconds
        h = [x for x in h if x["t"] >= t0]
        if len(h) <= max_points:
            return h
        step = len(h) / max_points
        return [h[int(i * step)] for i in range(max_points)] + [h[-1]]

    def snapshot(self):
        with self.lock:
            return self.current
