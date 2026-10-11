# SPDX-License-Identifier: BSD-3-Clause
"""The deployment's per-WAN settings (NATGW_WAN<n>_MAC, _ADDRESS, _ROUTES in
deploy.env, rendered into natgw.cli) on the site of site_env.py:

- WAN1 as AT&T Fiber through a WAS-110: the BGW320's MAC cloned onto the
  interface, and VPP at 192.168.11.2/24 next to the stick's LCT at
  192.168.11.1, whose management gateway is 192.168.11.2 (8311_gateway);
- WAN2 as a cable modem whose status page is 192.168.100.1, routed out of it.

LAN clients reach both pages through the WANs' NAT, and internet traffic,
offload and failover are unchanged."""

import time

import pytest

from site_env import Site, vppenv, wait_for

CLONED_MAC = "00:1a:2b:3c:4d:5e"
WAN_CLI = [f"set interface mac address NatgwModel1 {CLONED_MAC}",
           "set interface ip address NatgwModel1 192.168.11.2/24",
           "ip route add 192.168.100.1/32 via NatgwModel2"]


@pytest.fixture(scope="module")
def site():
    reason = vppenv.skip_reason()
    if reason:
        pytest.skip(reason)
    s = Site(wan_cli=WAN_CLI)
    lct = None
    try:
        wait_for(lambda: s.wan_state("wan1") == "up" and s.wan_state("wan2") == "up", timeout=40,
                 what="both WANs up")
        # the stick's LCT: a host on WAN1's segment, gateway VPP's address
        lct = vppenv.Netns("wmgr_lct")
        s.ns["lct"] = lct
        s.ns["wan1"].run("ip", "link", "add", "lct", "link", "eth0", "type", "macvlan", "mode", "bridge")
        s.ns["wan1"].run("ip", "link", "set", "lct", "netns", "wmgr_lct")
        lct.run("ip", "addr", "add", "192.168.11.1/24", "dev", "lct")
        lct.run("ip", "link", "set", "lct", "up")
        lct.run("ip", "link", "set", "lo", "up")
        lct.run("ip", "route", "add", "default", "via", "192.168.11.2")
        s.procs.append(lct.popen("python3", "-m", "http.server", "--bind", "192.168.11.1", "80"))
        # the modem's status page on WAN2's segment
        s.ns["wan2"].run("ip", "addr", "add", "192.168.100.1/32", "dev", "lo")
        s.procs.append(s.ns["wan2"].popen("python3", "-m", "http.server", "--bind", "192.168.100.1", "80"))
        wait_for(lambda: get(s, "http://192.168.11.1/") == "200", timeout=10, what="the LCT's web server")
    except BaseException:
        print(s.manager_log())
        s.close()
        raise
    yield s
    s.close()


def get(s, url):
    r = s.lan.run("curl", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}", url, check=False)
    return r.stdout.strip()


def peers(r):
    return {p["peer"] for p in r["peers"]}


def test_dhcp_uses_the_cloned_mac(site):
    with open(f"{site.tmp}/wan1.leases") as f:
        assert CLONED_MAC in f.read()
    assert CLONED_MAC in site.vpp.cli("show hardware-interfaces NatgwModel1")


def test_lan_reaches_the_ont_and_the_modem(site):
    assert get(site, "http://192.168.11.1/") == "200"
    assert get(site, "http://192.168.100.1/") == "200"


def test_internet_traffic_is_unchanged(site):
    r = site.udp(flows=16, count=4)
    assert r["lost"] == 0
    assert peers(r) == {site.lease("wan1"), site.lease("wan2")}, "only the leases are NAT addresses"
    assert site.nat_pool() == sorted([site.lease("wan1"), site.lease("wan2")])
    wait_for(lambda: site.offloaded() >= 16, timeout=10, what="the sessions offloaded")


def test_failover_keeps_the_ont_reachable(site):
    site.clear_sessions()
    site.upstream("wan1", False)
    try:
        wait_for(lambda: site.wan_state("wan1") == "down", timeout=20, what="wan1 down")
        r = site.udp(flows=8, count=4)
        assert r["lost"] == 0 and peers(r) == {site.lease("wan2")}
        assert get(site, "http://192.168.11.1/") == "200", "the ONT stays reachable while its ISP is down"
    finally:
        site.upstream("wan1", True)
    wait_for(lambda: site.wan_state("wan1") == "up", timeout=30, what="wan1 up")
    time.sleep(0.5)
    r = site.udp(flows=16, count=4)
    assert r["lost"] == 0 and peers(r) == {site.lease("wan1"), site.lease("wan2")}
