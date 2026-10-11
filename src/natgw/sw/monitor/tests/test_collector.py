# SPDX-License-Identifier: BSD-3-Clause
"""Collector against fake sources: rates, derived values, alerts, ISP
outages and their persistence."""

import json
import sqlite3
import time

import pytest

from monfakes import Clock, FakeStats, FakeWans
from natgw_monitor import config
from natgw_monitor.collector import Collector, Rates
from natgw_monitor.sources import SourceDown, StatsSource


def make(tmp_path, extra=""):
    clock = Clock()
    cfg = config.parse(f"""
[tls]
enabled = false
[store]
state_dir = "{tmp_path}"
history_seconds = 600
[alerts]
hold_seconds = 30
onchip_fill_seconds = 10
wanmgr_stale_seconds = 10
{extra}
""")
    stats, wans = FakeStats(), FakeWans(clock)
    return Collector(cfg, stats, wans, clock=clock), stats, wans, clock


def step(col, clock, wans, n=1, dt=1.0):
    cur = None
    for _ in range(n):
        clock.t += dt
        wans.tick()
        cur = col.step()
    return cur


def alert_ids(cur):
    return {a["id"] for a in cur["alerts"]}


def events(tmp_path):
    db = sqlite3.connect(tmp_path / "monitor.db")
    return [json.loads(r[0]) for r in db.execute("SELECT data FROM events ORDER BY id")]


# ---------------------------------------------------------------- rates

def test_rates():
    r = Rates()
    assert r.rate("a", 100, 10.0) is None
    assert r.rate("a", 300, 12.0) == 100.0
    assert r.rate("a", 50, 13.0) is None, "a counter going back (restart) has no rate"
    assert r.rate("a", 150, 14.0) == 100.0
    # 32-bit wrap
    assert r.rate("w", 2**32 - 10, 1.0, wrap32=True) is None
    assert r.rate("w", 30, 2.0, wrap32=True) == 40.0


def test_traffic_offload_ratio_and_reasons(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    s.bump("NatgwModel0", rx_forwarded=900, punt_miss=60, punt_syn=40, punt_bad_fcs=5)
    s.bump("NatgwModel1", rx_forwarded=1000)
    cur = step(col, clock, wans, dt=2.0)
    t = cur["traffic"]
    assert t["hw_pps"] == 950.0 and t["punt_pps"] == 50.0 and t["drop_pps"] == 2.5
    assert t["offload_pct"] == 95.0
    assert t["punt_reasons"]["miss"] == 30.0 and t["punt_reasons"]["syn"] == 20.0
    p0 = cur["ports"][0]
    assert p0["name"] == "NatgwModel0" and p0["rates"]["punt_miss"] == 30.0
    assert p0["link"] == {"admin_up": True, "link_up": True}


def test_first_sample_has_no_rates(tmp_path):
    col, s, wans, clock = make(tmp_path)
    cur = step(col, clock, wans)
    assert cur["traffic"]["hw_pps"] is None and cur["traffic"]["offload_pct"] is None


def test_device_tables_and_ddr(tmp_path):
    col, s, wans, clock = make(tmp_path)
    s.set_dev(flows=900, ddr_flows=100)
    step(col, clock, wans)
    s.bump("NatgwModel0", ddr_lookups=200, ddr_hits=150, promotions=4)
    d = step(col, clock, wans)["device"]
    assert d["onchip_flows"] == 800 and d["onchip_pct"] == 80.0 and d["ddr_pct"] == 2.5
    assert d["ddr_status"] == {"present": True, "calibrated": True, "enabled": True, "clearing": False,
                               "active": True}
    assert d["ddr_hit_pct"] == 75.0 and d["promotions_ps"] == 4.0


def test_ddr_counters_wrap(tmp_path):
    col, s, wans, clock = make(tmp_path)
    s.set_dev(ddr_lookups=2**32 - 100)
    step(col, clock, wans)
    s.set_dev(ddr_lookups=100)
    assert step(col, clock, wans)["device"]["ddr_lookups_ps"] == 200.0


def test_vpp_restart_resets_rates(tmp_path):
    col, s, wans, clock = make(tmp_path)
    s.bump("NatgwModel0", rx_forwarded=1000)
    step(col, clock, wans, 2)
    s.raw["boottime"] = 2000.0
    s.raw["xstats"]["NatgwModel0"]["rx_forwarded"] = 10
    cur = step(col, clock, wans)
    assert cur["traffic"]["hw_pps"] is None
    assert any(e["type"] == "vpp" and e["to"] == "restarted" for e in events(tmp_path))


# ---------------------------------------------------------------- alerts

def test_vpp_down_alert_and_event(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    s.down = "no stats socket"
    cur = step(col, clock, wans)
    assert "vpp_down" in alert_ids(cur) and not cur["vpp"]["up"]
    s.down = None
    cur = step(col, clock, wans)
    assert "vpp_down" not in alert_ids(cur)
    kinds = [(e["type"], e.get("to") or e.get("state")) for e in events(tmp_path)]
    assert ("vpp", "down") in kinds and ("vpp", "up") in kinds
    assert ("alert", "raised") in kinds and ("alert", "cleared") in kinds


def test_rise_alerts_hold_then_clear(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    s.bump("NatgwModel1", punt_bad_fcs=3)
    s.bump("NatgwModel0", ddr_read_errors=1)
    s.raw["offload"]["table_full"] += 7
    cur = step(col, clock, wans)
    ids = alert_ids(cur)
    assert {"bad_fcs:NatgwModel1", "ddr_read_errors", "table_full"} <= ids
    a = {x["id"]: x for x in cur["alerts"]}
    assert a["ddr_read_errors"]["severity"] == "critical" and a["table_full"]["detail"].startswith("7 ")
    cur = step(col, clock, wans, 20)
    assert "table_full" in alert_ids(cur), "held after the last rise"
    cur = step(col, clock, wans, 15)
    assert not {"bad_fcs:NatgwModel1", "ddr_read_errors", "table_full"} & alert_ids(cur)


def test_counters_present_at_start_do_not_alert(tmp_path):
    col, s, wans, clock = make(tmp_path)
    s.set_dev(events_lost=50, migrate_failures=3)
    step(col, clock, wans, 3)
    assert not alert_ids(col.snapshot())


def test_fill_alert_needs_time(tmp_path):
    col, s, wans, clock = make(tmp_path)
    s.set_dev(flows=900)
    cur = step(col, clock, wans, 5)
    assert "onchip_fill" not in alert_ids(cur)
    cur = step(col, clock, wans, 6)
    assert "onchip_fill" in alert_ids(cur)
    s.set_dev(flows=500)
    assert "onchip_fill" not in alert_ids(step(col, clock, wans))


def test_link_offload_and_ddr_alerts(tmp_path):
    col, s, wans, clock = make(tmp_path)
    s.raw["link"]["NatgwModel0"] = 1           # admin up, link down
    s.raw["link"]["NatgwModel2"] = 0           # admin down: no alert
    s.raw["offload"]["enabled"] = 0
    s.set_dev(ddr_status=0x1, ddr_capacity=0)  # present, no DIMM
    ids = alert_ids(step(col, clock, wans))
    assert {"link:NatgwModel0", "offload_disabled", "ddr_uncalibrated"} <= ids
    assert "link:NatgwModel2" not in ids


# ---------------------------------------------------------------- ISPs

def test_wan_down_alert_outage_and_snapshot(tmp_path):
    col, s, wans, clock = make(tmp_path)
    for _ in range(5):
        s.if_add("NatgwModel1", rx=10)
        s.bump("NatgwModel1", rx_forwarded=90)
        step(col, clock, wans)
    t_down = clock.t + 1
    clock.t += 1
    wans.set_state("wan1", "down", "no reply from probe targets (192.0.2.1)")
    clock.t -= 1
    cur = step(col, clock, wans)
    w1 = {w["name"]: w for w in cur["wans"]}["wan1"]
    assert w1["state"] == "down" and w1["outage"]["start"] == t_down
    a = {x["id"]: x for x in cur["alerts"]}
    assert a["wan_down:wan1"]["severity"] == "warning" and "traffic on wan2" in a["wan_down:wan1"]["detail"]
    o = cur["outages"][0]
    assert o["wan"] == "wan1" and o["end"] is None and o["reason"].startswith("no reply")
    snap = o["snapshot"]
    assert snap["others"] == {"wan2": "up"} and snap["probe_targets"]["192.0.2.1"]["ok"] is False
    assert snap["rx_pps_before"] == 100.0, "inbound before the outage: 90 FPGA + 10 VPP per second"
    # recovers 41 s later
    step(col, clock, wans, 41)
    wans.set_state("wan1", "up", "probe target 192.0.2.1 answering")
    cur = step(col, clock, wans)
    assert "wan_down:wan1" not in alert_ids(cur)
    o = cur["outages"][0]
    assert o["end"] == wans.st["wans"]["wan1"]["since"] and o["duration"] == pytest.approx(41.0)
    ev = events(tmp_path)
    assert [e["type"] for e in ev if e["type"].startswith("outage")] == ["outage_start", "outage_end"]
    assert [e["to"] for e in ev if e["type"] == "wan"] == ["down", "up"]


def test_all_wans_down_is_critical(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    wans.set_state("wan1", "down", "link down")
    wans.set_state("wan2", "down", "no DHCP lease")
    cur = step(col, clock, wans)
    a = {x["id"]: x for x in cur["alerts"]}
    assert a["all_wans_down"]["severity"] == "critical" and a["wan_down:wan1"]["severity"] == "critical"
    assert cur["alerts"][0]["severity"] == "critical"


def test_outage_history_survives_a_restart(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    wans.set_state("wan1", "down", "link down")
    step(col, clock, wans, 3)
    # the monitor restarts while wan1 is down, and wan1 recovers meanwhile
    clock.t += 20
    wans.set_state("wan1", "up", "probe target 192.0.2.1 answering")
    col2, _, _, _ = make(tmp_path)
    col2.stats, col2.wans, col2.clock = s, wans, clock
    col2.alerts.clock = clock
    cur = step(col2, clock, wans)
    o = cur["outages"][0]
    assert o["wan"] == "wan1" and o["end"] is not None and o["duration"] == pytest.approx(23.0)
    # wanmgr events already logged are not logged twice
    assert [e["to"] for e in events(tmp_path) if e["type"] == "wan"] == ["down", "up"]


def test_stale_wan_manager(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    clock.t += 30          # status file no longer updated
    cur = col.step()
    assert not cur["wanmgr"]["up"] and "not updated" in cur["wanmgr"]["error"]
    assert "wanmgr" in alert_ids(cur)
    assert all(w.get("stale") for w in cur["wans"])
    # no outage verdicts from a stale manager
    wans.st["wans"]["wan1"]["state"] = "down"
    assert col.step()["outages"] == []
    wans.down = "no status file (WAN manager not running?)"
    cur = col.step()
    assert cur["wans"] == [] and "not running" in cur["wanmgr"]["error"]


def test_wan_traffic(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans)
    s.if_add("NatgwModel1", rx=5, tx=7)
    s.bump("NatgwModel1", rx_forwarded=100)
    cur = step(col, clock, wans)
    t = {w["name"]: w for w in cur["wans"]}["wan1"]["traffic"]
    assert t == {"port": "NatgwModel1", "sw_rx_pps": 5.0, "sw_tx_pps": 7.0, "hw_rx_pps": 100.0, "rx_pps": 105.0}


def test_history_window_and_downsampling(tmp_path):
    col, s, wans, clock = make(tmp_path)
    step(col, clock, wans, 700)
    assert len(col.history) == 601
    h = col.history_since(60)
    assert len(h) == 61 and h[-1]["t"] == clock.t
    assert len(col.history_since(600, max_points=100)) == 101


# ---------------------------------------------------------------- StatsSource

class FakeClient:
    """the part of vpp_papi's VPPStats the source uses"""

    def __init__(self, data):
        self.data = data
        self.directory = {}
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    epoch = 1
    in_progress = 0
    fail = 0              # raise IOError this many times (VPP updating)

    def refresh(self, blocking=True):
        assert not blocking, "a blocking read can hang on a dead VPP"
        if self.fail:
            self.fail -= 1
            raise IOError("Optimistic lock failed, retry")
        with self.lock:
            self.directory = dict.fromkeys(self.data)

    def __getitem__(self, k, blocking=True):
        assert not blocking
        with self.lock:
            return self.data[k]


def test_stats_source_parses_the_segment(tmp_path):
    sock = tmp_path / "stats.sock"
    sock.write_text("")
    data = {
        "/sys/boottime": 5.0, "/natgw/heartbeat": 1, "/if/names": ["local0", "TwentyFiveGigabitEthernet3/0/0"],
        "/if/rx": [[(1, 10), (2, 20)], [(0, 0), (3, 30)]],
        "/if/drops": [[0, 4]],
        "/interfaces/TwentyFiveGigabitEthernet3/0/0/xstats-natgw_rx_forwarded": [7, 1],
        "/interfaces/TwentyFiveGigabitEthernet3/0/0/xstats-rx_good_packets": [9],
        "/natgw/offload/installed": 3, "/natgw/offloaded": 2,
        "/natgw/link/TwentyFiveGigabitEthernet3/0/0": 3,
        "/nat44-ed/total-sessions": [[5], [1]], "/nat44-ed/max-cfg-sessions": 100,
        "/sys/vector_rate": 1.5,
    }
    clients = []

    def factory(path):
        clients.append(FakeClient(data))
        return clients[-1]
    t = [0.0]
    src = StatsSource(str(sock), client_factory=factory, clock=lambda: t[0])
    r = src.read()
    port = "TwentyFiveGigabitEthernet3/0/0"
    assert r["xstats"] == {port: {"rx_forwarded": 8}}, "interface names with slashes, other xstats skipped"
    assert r["if_rx"] == [(1, 10), (5, 50)] and r["if_drops"] == [0, 4]
    assert r["offload"] == {"installed": 3, "offloaded": 2} and r["link"] == {port: 3}
    assert r["nat"] == {"total-sessions": 6, "max-cfg-sessions": 100} and r["sys"]["vector_rate"] == 1.5
    # the heartbeat stops: VPP hung; the next read maps the segment afresh
    t[0] = 10.0
    with pytest.raises(SourceDown, match="not updating"):
        src.read()
    assert not clients[0].connected
    data["/natgw/heartbeat"] = 2
    src.read()
    assert len(clients) == 2
    # VPP restarted (new boottime): a new client maps the new segment
    data["/sys/boottime"] = 6.0
    src.read()
    assert len(clients) == 3 and not clients[1].connected
    # a new socket file (even with the old inode): reconnect
    sock.unlink()
    time.sleep(0.05)     # file times are kernel ticks: VPP restarts take far longer
    sock.write_text("")
    src.read()
    assert len(clients) == 4
    # VPP busy updating: retried; stuck mid-update (it died): down, mapped afresh
    clients[-1].fail = 2
    src.read()
    clients[-1].in_progress = 1
    t0 = time.monotonic()
    with pytest.raises(SourceDown, match="in progress for too long"):
        src.read()
    assert time.monotonic() - t0 < 2 and not clients[-1].connected
    src.read()
    assert len(clients) == 5
    sock.unlink()
    with pytest.raises(SourceDown, match="no stats socket"):
        src.read()


# ---------------------------------------------------------------- config

def test_config_defaults_and_errors():
    c = config.parse("")
    assert c.http.bind == ["127.0.0.1"] and c.http.port == 8443 and c.tls.enabled
    c = config.parse('[http]\nbind = "enp1s0"\n')
    assert c.http.bind == ["enp1s0"]
    c = config.parse('[http]\nbind = ["192.168.1.2", "::1", "lo"]\nport = 9443\n')
    assert c.http.bind == ["192.168.1.2", "::1", "lo"]
    for text, msg in [
        ("[http]\nport = 70000\n", "port"),
        ("[http]\nbind = []\n", "bind"),
        ('[http]\nbind = ["not an/interface"]\n', "neither"),
        ('[tls]\ncert = ""\n', "cert and key"),
        ("[bogus]\n", "unknown sections"),
        ("[http]\nfoo = 1\n", "unknown keys"),
        ("[alerts]\nonchip_fill_pct = 0\n", "onchip_fill_pct"),
    ]:
        with pytest.raises(config.ConfigError, match=msg):
            config.parse(text)
