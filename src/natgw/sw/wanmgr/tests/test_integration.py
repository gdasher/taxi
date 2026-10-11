# SPDX-License-Identifier: BSD-3-Clause
"""The WAN manager against real VPP (with natgw_offload on the shim model),
on the site of site_env.py: a LAN and two WANs with DHCP 'modems'."""

import os
import time

import pytest

from site_env import INTERVAL, WANS, Site, vppenv, wait_for


@pytest.fixture(scope="module")
def site():
    reason = vppenv.skip_reason()
    if reason:
        pytest.skip(reason)
    if not os.path.exists(os.path.join(vppenv.PLUGIN_DIR, "dhcp_plugin.so")):
        pytest.skip("dhcp plugin not built")
    s = Site()
    try:
        wait_for(lambda: s.wan_state("wan1") == "up" and s.wan_state("wan2") == "up", timeout=40,
                 what="both WANs up")
    except AssertionError:
        print(s.manager_log())
        s.close()
        raise
    yield s
    s.close()


def addrs(s):
    return {n: s.lease(n) for n in WANS}


def test_startup_learns_leases_and_steers(site):
    s = site
    a = addrs(s)
    for n, w in WANS.items():
        lo, hi = w["pool"]
        assert a[n] and lo <= a[n] <= hi, a     # (same /24: string order is numeric order here)
    assert s.api_default_paths() == sorted(w["modem"] for w in WANS.values())
    assert s.nat_pool() == sorted(a.values())
    # probe targets are pinned to their WAN
    out = s.vpp.cli("show ip fib 192.0.2.1/32")
    assert "198.51.100.2 NatgwModel1" in out
    # LAN traffic leaves through both WANs with each WAN's own address
    r = s.udp(flows=16)
    assert r["received"] == r["sent"], r
    assert {p["peer"] for p in r["peers"]} == set(a.values())
    assert s.mails() == []


def test_wan_outage_steers_flushes_and_alerts(site):
    s = site
    a = addrs(s)
    s.clear_sessions()
    # long-lived offloaded flows on both WANs
    flows = s.udp(flows=16, count=6, gap=0.02)
    assert flows["received"] == flows["sent"]
    wait_for(lambda: s.offloaded() == 16, what="offloaded")
    on_wan1 = sum(1 for p in flows["peers"] if p["peer"] == a["wan1"])
    assert 0 < on_wan1 < 16
    t0 = time.time()
    s.upstream("wan1", False)
    try:
        wait_for(lambda: s.wan_state("wan1") == "down", timeout=15, what="wan1 down")
        took = time.time() - t0
        assert took >= 2 * INTERVAL, f"down after {took:.1f}s: fewer than 3 rounds"
        assert s.api_default_paths() == [WANS["wan2"]["modem"]]
        # WAN1's address stays in the pool (its probes need it); its sessions
        # are gone, and with them their hardware flows
        assert s.nat_pool() == sorted(a.values())
        sessions = s.vpp.cli("show nat44 sessions")
        assert a["wan1"] not in sessions
        wait_for(lambda: s.offloaded() == 16 - on_wan1, what="WAN1's flows removed from hardware")
        wait_for(lambda: "wan1 DOWN" in s.mails(), what="alert mail")
        # new traffic all goes out of WAN2 and works
        r = s.udp(flows=8)
        assert r["received"] == r["sent"], r
        assert {p["peer"] for p in r["peers"]} == {a["wan2"]}
    finally:
        s.upstream("wan1", True)
    wait_for(lambda: s.wan_state("wan1") == "up", timeout=15, what="wan1 back up")
    assert s.api_default_paths() == sorted(w["modem"] for w in WANS.values())
    assert s.nat_pool() == sorted(a.values())
    wait_for(lambda: "wan1 UP" in s.mails(), what="recovery mail")


def test_all_wans_down_falls_back_to_dhcp_routes(site):
    s = site
    a = addrs(s)
    for n in WANS:
        s.upstream(n, False)
    try:
        wait_for(lambda: s.wan_state("wan1") == "down" and s.wan_state("wan2") == "down", timeout=15,
                 what="both down")
        assert s.api_default_paths() == []
        out = s.vpp.cli("show ip fib 0.0.0.0/0")
        assert "DHCP" in out.upper() and "198.51.100.2" in out and "198.18.0.2" in out
        assert s.nat_pool() == sorted(a.values())
        wait_for(lambda: any("DOWN" in m for m in s.mails()), what="mail")
    finally:
        for n in WANS:
            s.upstream(n, True)
    wait_for(lambda: s.wan_state("wan1") == "up" and s.wan_state("wan2") == "up", timeout=15, what="both up")
    assert s.api_default_paths() == sorted(w["modem"] for w in WANS.values())


def test_vpp_restart_is_recovered(site):
    s = site
    s.vpp.restart()
    # the model's TAPs are new: rewire the namespaces and the modems
    for p in s.procs:
        p.kill()
    s.procs = []
    for n in WANS:
        s.ns[n].kill_all()
    s.wire()
    s.configure_vpp()
    wait_for(lambda: s.status() and s.status()["connected"] and s.api_default_paths() ==
             sorted(w["modem"] for w in WANS.values()), timeout=40, what="manager re-steered after restart")
    assert s.wan_state("wan1") == "up" and s.wan_state("wan2") == "up"
    assert s.nat_pool() == sorted(addrs(s).values())
    r = s.udp(flows=8)
    assert r["received"] == r["sent"], r
    wait_for(lambda: "VPP reconnected" in s.mails(), what="reconnect mail")
