# SPDX-License-Identifier: BSD-3-Clause
"""The monitor against the real thing: VPP with natgw_offload on the shim
model, the WAN manager, two ISPs (site_env.py from the WAN manager's
tests), and the monitor as a daemon serving HTTPS, run as the ordinary user
(it reads VPP's stats socket through group permissions, as the deployed
service does through the vpp group). The page is rendered in headless
Chromium when Playwright is installed."""

import http.client
import json
import os
import signal
import ssl
import subprocess
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.dirname(HERE)
SW = os.path.dirname(MONITOR)
sys.path.append(os.path.join(SW, "wanmgr", "tests"))

from site_env import WANS, Site, vppenv, wait_for  # noqa: E402
from certs import free_port, make_cert  # noqa: E402

pytestmark = pytest.mark.skipif(bool(vppenv.skip_reason()), reason=vppenv.skip_reason() or "")


class Monitor:
    def __init__(self, site, state_dir):
        self.dir = tempfile.mkdtemp(prefix="natgw-mon-")
        self.crt, self.key = make_cert(self.dir, "monitor")
        self.port = free_port()
        self.cfg = os.path.join(self.dir, "monitor.toml")
        with open(self.cfg, "w") as f:
            f.write(f"""
[http]
bind = ["127.0.0.1"]
port = {self.port}
[tls]
cert = "{self.crt}"
key = "{self.key}"
[sources]
stats_socket = "{site.vpp.stats_sock}"
wanmgr_status = "{site.status_path}"
[store]
state_dir = "{state_dir}"
[alerts]
hold_seconds = 20
""")
        self.log = open(os.path.join(self.dir, "monitor.log"), "w")
        self.p = None
        self.start()

    def start(self):
        env = dict(os.environ, PYTHONPATH=MONITOR)
        self.p = subprocess.Popen([sys.executable, "-m", "natgw_monitor", "-c", self.cfg, "-v"], env=env,
                                  stdout=self.log, stderr=subprocess.STDOUT)
        wait_for(lambda: self._ok("/healthz"), timeout=20, what="monitor serving")

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            self.p.wait(timeout=10)

    def _ok(self, path):
        try:
            return self.get(path)[0] == 200
        except OSError:
            return False

    def get(self, path):
        ctx = ssl.create_default_context(cafile=self.crt)
        c = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def stats(self):
        code, body = self.get("/api/stats")
        return json.loads(body) if code == 200 else None

    def wan(self, name):
        st = self.stats()
        return st and {w["name"]: w for w in st["wans"]}.get(name)

    def alerts(self):
        st = self.stats()
        return {a["id"]: a for a in st["alerts"]} if st else {}

    def output(self):
        with open(os.path.join(self.dir, "monitor.log")) as f:
            return f.read()[-4000:]


@pytest.fixture(scope="module")
def env():
    if not os.path.exists(os.path.join(vppenv.PLUGIN_DIR, "dhcp_plugin.so")):
        pytest.skip("dhcp plugin not built")
    site = Site(name="wmon", dpdk_extra="stats-poll-interval 1")
    state = tempfile.mkdtemp(prefix="natgw-mon-state-")
    mon = None
    try:
        wait_for(lambda: site.wan_state("wan1") == "up" and site.wan_state("wan2") == "up", timeout=40,
                 what="both WANs up")
        mon = Monitor(site, state)
        yield site, mon, state
    except BaseException:
        print(site.manager_log())
        if mon:
            print(mon.output())
        raise
    finally:
        if mon:
            mon.stop()
        site.close()


def cli_xstats(site, port):
    out = site.vpp.cli(f"show hardware-interfaces {port} detail")
    x = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].startswith("natgw_") and f[1].isdigit():
            x[f[0][len("natgw_"):]] = int(f[1])
    return x


def test_sees_the_gateway(env):
    site, mon, _ = env
    st = wait_for(lambda: (s := mon.stats()) and s["wanmgr"]["up"] and s, what="first sample")
    assert st["vpp"]["up"] and [p["name"] for p in st["ports"]] == ["NatgwModel0", "NatgwModel1", "NatgwModel2"]
    assert all(p["link"]["admin_up"] and p["link"]["link_up"] for p in st["ports"])
    assert st["offload"]["enabled"] is True
    cap = cli_xstats(site, "NatgwModel0")["onchip_capacity"]
    assert cap > 0 and st["device"]["onchip_capacity"] == cap
    assert st["device"]["ddr_in_use"] is False and st["device"]["ddr_status"]["present"] is False
    w = {x["name"]: x for x in st["wans"]}
    for name, cfg in WANS.items():
        assert w[name]["state"] == "up" and w[name]["in_route"] and w[name]["interface"] == cfg["if"]
        assert w[name]["lease"]["gateway"] == cfg["modem"]
        assert w[name]["traffic"]["port"] == cfg["if"]
    assert st["alerts"] == [], st["alerts"]


def test_traffic_counters_match_vpp(env):
    site, mon, _ = env
    site.clear_sessions()
    r = site.udp(flows=16, count=20, gap=0.05)
    assert r["received"] == r["sent"]
    wait_for(lambda: site.offloaded() == 16, what="offloaded")

    def agree():
        st = mon.stats()
        off = site.vpp.offload_counters()
        xs = {p: cli_xstats(site, p) for p in ("NatgwModel0", "NatgwModel1", "NatgwModel2")}
        ok = (st["offload"]["offloaded"] == 16 and
              st["offload"]["counters"]["installed"] == off["sessions offloaded"] and
              all(p["natgw"]["rx_forwarded"] == xs[p["name"]]["rx_forwarded"] and
                  p["natgw"]["punt_miss"] == xs[p["name"]]["punt_miss"] for p in st["ports"]))
        return ok and st
    st = wait_for(agree, timeout=10, interval=0.5, what="monitor counters equal VPP's")
    fwd = sum(p["natgw"]["rx_forwarded"] for p in st["ports"])
    assert fwd > 0, "the FPGA (model) forwarded the offloaded flows"
    hist = json.loads(mon.get("/api/history?seconds=120")[1])["samples"]
    assert max(h["hw"] or 0 for h in hist) > 0 and max(h["ratio"] or 0 for h in hist) > 0
    assert any((h["wans"]["wan1"]["rx"] or 0) > 0 for h in hist), "per-ISP inbound traffic"
    assert st["nat"]["sessions"] >= 16


def test_isp_outage_is_captured(env):
    site, mon, state = env
    t0 = time.time()
    site.upstream("wan1", False)
    try:
        w = wait_for(lambda: (x := mon.wan("wan1")) and x["state"] == "down" and x, timeout=20,
                     what="monitor shows wan1 down")
        assert w["reason"] == "no reply from probe targets (192.0.2.1, 192.0.2.2)"
        assert not w["in_route"] and w["lease"] is not None and w["outage"]["start"] >= t0
        assert all(r["ok"] is False for r in w["probe_targets"].values())
        assert w["history"].endswith("000")
        a = mon.alerts()["wan_down:wan1"]
        assert a["severity"] == "warning" and "traffic on wan2" in a["detail"]
        st = mon.stats()
        o = st["outages"][0]
        assert o["wan"] == "wan1" and o["end"] is None
        snap = o["snapshot"]
        assert snap["others"] == {"wan2": "up"} and snap["lease"]["gateway"] == WANS["wan1"]["modem"]
        assert snap["link"] == {"admin_up": True, "link_up": True}
        assert st["wanmgr"]["route"] and len(st["wanmgr"]["route"]) == 1
        down_at = w["since"]
    finally:
        site.upstream("wan1", True)
    w = wait_for(lambda: (x := mon.wan("wan1")) and x["state"] == "up" and x, timeout=20, what="wan1 back up")
    st = mon.stats()
    o = [x for x in st["outages"] if x["wan"] == "wan1"][0]
    assert o["end"] == w["since"] and abs(o["duration"] - (w["since"] - down_at)) < 0.01
    assert 5 <= o["duration"] <= 30
    assert "wan_down:wan1" not in mon.alerts()
    with open(os.path.join(state, "events.jsonl")) as f:
        ev = [json.loads(x) for x in f]
    assert [e["type"] for e in ev if e["type"].startswith("outage")][-2:] == ["outage_start", "outage_end"]
    assert any(e["type"] == "alert" and e["id"] == "wan_down:wan1" and e["state"] == "cleared" for e in ev)
    # the history survives a monitor restart
    mon.stop()
    mon.start()
    o2 = [x for x in mon.stats()["outages"] if x["wan"] == "wan1"][0]
    assert o2["start"] == o["start"] and o2["end"] == o["end"]


def test_all_isps_down_is_critical(env):
    site, mon, _ = env
    for n in WANS:
        site.upstream(n, False)
    try:
        a = wait_for(lambda: (x := mon.alerts()) and "all_wans_down" in x and x, timeout=20, what="all down")
        assert a["all_wans_down"]["severity"] == "critical"
        assert a["wan_down:wan1"]["severity"] == "critical" and a["wan_down:wan2"]["severity"] == "critical"
        assert mon.stats()["wanmgr"]["route"] == []
    finally:
        for n in WANS:
            site.upstream(n, True)
    wait_for(lambda: not ({"all_wans_down", "wan_down:wan1", "wan_down:wan2"} & set(mon.alerts())), timeout=25,
             what="alerts cleared")


def test_page_renders(env, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    site, mon, _ = env
    site.upstream("wan2", False)
    try:
        wait_for(lambda: (x := mon.wan("wan2")) and x["state"] == "down", timeout=20, what="wan2 down")
        errors = []
        with pw.sync_playwright() as p:
            b = p.chromium.launch()
            for width, scheme in ((1280, "light"), (390, "dark")):
                page = b.new_page(ignore_https_errors=True, viewport={"width": width, "height": 900},
                                  color_scheme=scheme)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: m.type == "error" and errors.append(m.text))
                page.goto(f"https://127.0.0.1:{mon.port}/")
                page.wait_for_selector("article.wan.down[data-wan=wan2]", timeout=10000)
                page.wait_for_function("document.querySelectorAll('#events tbody tr').length > 1", timeout=15000)
                card = page.inner_text("article.wan.down[data-wan=wan2]")
                assert "Down for" in card and "no reply from probe targets" in card
                assert "wan2 DOWN" in page.inner_text("#banner")
                assert "wan2" in page.inner_text("#alerts")
                assert "ongoing" in page.inner_text("#outages")
                assert page.inner_text("#overall") != "all good"
                # no horizontal scrolling of the page itself
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                page.screenshot(path=str(tmp_path / f"page-{width}-{scheme}.png"), full_page=True)
                page.close()
            b.close()
        assert errors == [], errors
        print(f"screenshots in {tmp_path}")
    finally:
        site.upstream("wan2", True)
    wait_for(lambda: (x := mon.wan("wan2")) and x["state"] == "up", timeout=20, what="wan2 up")


def test_vpp_restart(env):
    site, mon, _ = env
    boot = mon.stats()["vpp"]["boottime"]
    site.vpp.restart()
    for p in site.procs:
        p.kill()
    site.procs = []
    for n in WANS:
        site.ns[n].kill_all()
    site.wire()
    site.configure_vpp()

    def back():
        st = mon.stats()
        return st and st["vpp"]["up"] and st["vpp"]["boottime"] != boot and st["wanmgr"]["up"] and \
            all(w["state"] == "up" for w in st["wans"]) and len(st["ports"]) == 3 and st
    wait_for(back, timeout=60, interval=0.5, what="monitor sees VPP and both WANs back")
    ev = json.loads(mon.get("/api/events?limit=500")[1])["events"]
    if not any(e["type"] == "vpp" and e["to"] in ("down", "restarted") for e in ev):
        with open(os.path.join(mon.dir, "events.json"), "w") as f:
            json.dump(ev, f, indent=1)
        mon.p.send_signal(signal.SIGUSR1)
        time.sleep(1)
        pytest.fail(f"no VPP restart event; see {mon.dir}")
    # counters restarted at zero: no negative rates
    r = site.udp(flows=4, count=10, gap=0.05)
    assert r["received"] == r["sent"]
    time.sleep(2)
    hist = json.loads(mon.get("/api/history?seconds=60")[1])["samples"]
    assert all((h["hw"] is None or h["hw"] >= 0) and (h["punt"] is None or h["punt"] >= 0) for h in hist)
