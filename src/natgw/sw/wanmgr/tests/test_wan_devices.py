# SPDX-License-Identifier: BSD-3-Clause
"""The deployment's per-WAN settings on the site of site_env.py, with VPP
running the WAN lines and the WAN ingress ACL of the natgw.cli that
natgw-guest-setup.sh renders from deploy.env.example (sw/deploy/tests/
guest_render.py), lanes renamed to the model's:

- WAN1 as AT&T Fiber through a WAS-110: the BGW320's MAC cloned onto the
  interface, and VPP at 192.168.11.2/24 next to the stick's LCT at
  192.168.11.1, whose management gateway is 192.168.11.2 (8311_gateway);
- WAN2 as a cable modem whose status page is 192.168.100.1, routed out of it.

LAN clients reach both pages through the WANs' NAT; nothing on the ISP side
reaches them through VPP, even with NAT forwarding on (the ACL); internet
traffic, offload and failover are unchanged."""

import os
import re
import sys
import time

import pytest

from site_env import SW, Site, vppenv, wait_for

sys.path.insert(0, os.path.join(SW, "deploy", "tests"))
from guest_render import render  # noqa: E402

CLONED_MAC = "00:1a:2b:3c:4d:5e"
LCT, MODEM = "192.168.11.1", "192.168.100.1"
PROBE_PORT = 9999
# a device's UDP sink: logs the source of every datagram, so a test sees
# whether a packet arrived whatever happens to replies
SINK = """
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind((sys.argv[1], int(sys.argv[2])))
print("READY", flush=True)
while True:
    d, a = s.recvfrom(2048)
    with open(sys.argv[3], "a") as f:
        f.write(d.decode(errors="replace") + "\\n")
"""


def deployed_cli():
    """natgw.cli's WAN lines and WAN ACL, for the model's interfaces"""
    cli = render(f"NATGW_WAN1_MAC={CLONED_MAC}", "NATGW_WAN1_IF=NatgwModel1",
                 "NATGW_WAN2_IF=NatgwModel2")["natgw.cli"]
    keep = ("set interface mac address NatgwModel", "set interface ip address NatgwModel", "ip route add",
            "set acl-plugin")
    return [ln for ln in cli.splitlines() if ln.startswith(keep)]


def device_ns(s, wan, name, cidr, route):
    """a host on a WAN's segment (macvlan on the 'modem' namespace's wire)"""
    ns = vppenv.Netns(f"wmgr_{name}")
    s.ns[name] = ns
    s.ns[wan].run("ip", "link", "add", name, "link", "eth0", "type", "macvlan", "mode", "bridge")
    s.ns[wan].run("ip", "link", "set", name, "netns", f"wmgr_{name}")
    ns.run("ip", "addr", "add", cidr, "dev", name)
    ns.run("ip", "link", "set", name, "up")
    ns.run("ip", "link", "set", "lo", "up")
    ns.run("ip", "route", "add", "default", *route)
    addr = cidr.split("/")[0]
    s.procs.append(ns.popen("python3", "-m", "http.server", "--bind", addr, "80"))
    sink = ns.popen("python3", "-c", SINK, addr, str(PROBE_PORT), os.path.join(s.tmp, f"{name}.rx"))
    assert sink.stdout.readline().startswith("READY")
    s.procs.append(sink)
    return ns


@pytest.fixture(scope="module")
def site():
    reason = vppenv.skip_reason()
    if reason:
        pytest.skip(reason)
    cli = deployed_cli()
    assert any(ln.startswith("set acl-plugin acl ") for ln in cli)
    s = Site(wan_cli=cli, extra_plugins=["acl_plugin.so"])
    try:
        wait_for(lambda: s.wan_state("wan1") == "up" and s.wan_state("wan2") == "up", timeout=40,
                 what="both WANs up")
        # the stick's LCT, gateway VPP's address; the modem, which answers its
        # router (the lease) on the segment
        device_ns(s, "wan1", "lct", f"{LCT}/24", ["via", "192.168.11.2"])
        device_ns(s, "wan2", "mdm", f"{MODEM}/24", ["dev", "mdm"])
        wait_for(lambda: get(s.lan, f"http://{LCT}/") == "200", timeout=10, what="the LCT's web server")
    except BaseException:
        print(s.manager_log())
        s.close()
        raise
    yield s
    s.close()


def get(ns, url):
    r = ns.run("curl", "-s", "-m", "2", "-o", "/dev/null", "-w", "%{http_code}", url, check=False)
    return r.stdout.strip()


def peers(r):
    return {p["peer"] for p in r["peers"]}


def arrived(s, dev, tag):
    try:
        with open(os.path.join(s.tmp, f"{dev}.rx")) as f:
            return tag in f.read().split()
    except FileNotFoundError:
        return False


SEQ = iter(range(1, 1 << 30))


def from_isp(s, wan, dst, dev):
    """UDP datagrams to dst from the ISP side of a WAN, with VPP's lease as
    the next hop (as anyone routing towards the gateway would send them):
    True if any arrived at the device"""
    tag = f"probe-{wan}-{next(SEQ)}"
    ns = s.ns[wan]
    ns.run("ip", "route", "replace", f"{dst}/32", "via", s.lease(wan))
    try:
        for _ in range(3):
            ns.run("python3", "-c", "import socket,sys; socket.socket(2, 2).sendto(sys.argv[1].encode(), "
                   "(sys.argv[2], int(sys.argv[3])))", tag, dst, str(PROBE_PORT))
            time.sleep(0.2)
        time.sleep(0.3)
        return arrived(s, dev, tag)
    finally:
        ns.run("ip", "route", "del", f"{dst}/32", check=False)


def acl_denied(s):
    m = re.search(r"(\d+)\s+acl-plugin-in-ip4-fa\s+ACL deny packets", s.vpp.cli("show errors"))
    return int(m.group(1)) if m else 0


def test_dhcp_uses_the_cloned_mac(site):
    def leased():
        with open(f"{site.tmp}/wan1.leases") as f:
            return CLONED_MAC in f.read()
    wait_for(leased, timeout=5, what="the lease under the cloned MAC")
    assert CLONED_MAC in site.vpp.cli("show hardware-interfaces NatgwModel1")


def test_lan_reaches_the_ont_and_the_modem(site):
    assert get(site.lan, f"http://{LCT}/") == "200"
    assert get(site.lan, f"http://{MODEM}/") == "200"


def test_the_stick_reaches_vpp(site):
    """the 8311 firmware pings 192.168.11.2: the ACL lets it through"""
    r = site.ns["lct"].run("ping", "-c", "2", "-W", "1", "192.168.11.2", check=False)
    assert " 0% packet loss" in r.stdout


def test_the_sinks_work(site):
    """the probes' own control: datagrams from the LAN arrive"""
    for dst, dev in ((LCT, "lct"), (MODEM, "mdm")):
        site.lan.run("python3", "-c", "import socket,sys; socket.socket(2, 2).sendto(b'lan-probe', "
                     "(sys.argv[1], int(sys.argv[2])))", dst, str(PROBE_PORT))
        wait_for(lambda: arrived(site, dev, "lan-probe"), timeout=3, what=f"a LAN datagram at {dev}")


def test_the_isp_side_cannot_reach_the_devices(site):
    before = acl_denied(site)
    for wan in ("wan1", "wan2"):
        for dst, dev in ((LCT, "lct"), (MODEM, "mdm")):
            assert not from_isp(site, wan, dst, dev), f"{dst} reached from {wan}'s ISP side"
    assert acl_denied(site) > before


def test_the_acl_holds_with_nat_forwarding_on(site):
    """with "nat44 forwarding enable", NAT passes packets it has no session
    for: only the ACL keeps the ISP side off the devices"""
    v = site.vpp
    v.cli("nat44 forwarding enable")
    try:
        assert not from_isp(site, "wan1", LCT, "lct")
        assert not from_isp(site, "wan2", MODEM, "mdm")
        assert not from_isp(site, "wan1", MODEM, "mdm")
        # control: without the ACL on WAN1, the stick is on the internet
        v.cli("set acl-plugin interface NatgwModel1 input acl 0 del")
        try:
            assert from_isp(site, "wan1", LCT, "lct"), "the control: NAT forwarding passes it without the ACL"
        finally:
            v.cli("set acl-plugin interface NatgwModel1 input acl 0")
        assert not from_isp(site, "wan1", LCT, "lct")
    finally:
        v.cli("nat44 forwarding disable")
    assert get(site.lan, f"http://{LCT}/") == "200"


def test_internet_traffic_is_unchanged(site):
    site.clear_sessions()
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
        assert get(site.lan, f"http://{LCT}/") == "200", "the ONT stays reachable while its ISP is down"
    finally:
        site.upstream("wan1", True)
    wait_for(lambda: site.wan_state("wan1") == "up", timeout=30, what="wan1 up")
    time.sleep(0.5)
    r = site.udp(flows=16, count=4)
    assert r["lost"] == 0 and peers(r) == {site.lease("wan1"), site.lease("wan2")}
