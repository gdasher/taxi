# SPDX-License-Identifier: BSD-3-Clause
"""VPP nat44-ed + natgw_offload against the shim model (net_natgw_model,
wire=tap): kernel TCP/UDP endpoints in namespaces on each lane.

What 'offloaded' means here is observable: once a session is offloaded its
packets are forwarded by the model and VPP's interfaces stop counting them,
while VPP's session counters still grow (synced back from hardware)."""

import time

import pytest

from conftest import (LAN_HOST, SERVER, WAN1_ADDR, WAN1_GW, WAN2_ADDR, WAN2_GW, Gateway, vppenv,
                      wait_for)

PAUSE = 0.3  # > the offload process' event interval, with margin


def udp_args(flows=1, count=10, phases=2, gap=0.0, size=100):
    return ["udp-client", "--dst", SERVER, "--port", "5000", "--flows", str(flows), "--count", str(count),
            "--phases", str(phases), "--gap", str(gap), "--size", str(size)]


def test_udp_offloaded_after_active_threshold(fresh):
    gw = fresh
    seen = {}

    def between(phase):
        # phase 1: 6 round trips through VPP; the 4th packet raised ACTIVE
        wait_for(lambda: gw.offloaded() == 1, what="session offloaded")
        seen["rx"] = gw.lan_rx()

    r = gw.client_phased(udp_args(count=6, phases=2), between)
    # phase 2 ran another 6 round trips (12 packets) entirely in hardware
    assert r["received"] == r["sent"] == 12
    assert gw.lan_rx() - seen["rx"] <= 1, "offloaded packets reached VPP"
    c = gw.offload()
    assert c["sessions offloaded"] == 1 and c["events"] >= 1
    # translation as VPP would do it: the server saw the WAN address
    assert r["peers"][0]["peer"] == WAN1_ADDR
    # hardware counts folded into the VPP session
    gw.cli("natgw offload sync")
    (s,) = gw.sessions()
    assert s["pkts"] == 24, s
    assert gw.xstat("natgw_rx_forwarded") >= 6


def test_udp_below_threshold_stays_in_software(fresh):
    gw = fresh
    r = gw.client(*udp_args(count=1, phases=1))
    assert r["received"] == 1
    time.sleep(PAUSE)
    assert gw.offloaded() == 0 and gw.offload()["events"] == 0
    assert len(gw.sessions()) == 1


def test_tcp_offloaded_after_handshake_and_removed_on_fin(fresh):
    gw = fresh
    seen = {}

    def between(phase):
        if phase == 1:
            wait_for(lambda: gw.offloaded() == 1, what="TCP session offloaded")
            seen["rx"] = gw.lan_rx()
        else:
            seen["rx2"] = gw.lan_rx()

    args = ["tcp-client", "--dst", SERVER, "--port", "6000", "--bytes", str(3 * 200_000), "--phases", "3",
            "--echo", "--chunk", "4096"]
    r = gw.client_phased(args, between)
    assert r["server"]["bytes"] == r["sent"] == 600_000
    assert r["server"]["sha"] == r["sha"] and r["echoed"] == r["sent"]
    assert r["server"]["peer"] == WAN1_ADDR
    # phase 2 (~150 segments each way plus ACKs) bypassed VPP
    assert seen["rx2"] - seen["rx"] <= 2, (seen, "offloaded TCP reached VPP")
    # FIN/RST are punted: VPP sees the close and the flows are removed
    wait_for(lambda: gw.offloaded() == 0, what="flows removed after FIN")
    c = gw.offload()
    assert c["sessions offloaded"] == 1 and c["sessions removed"] == 1
    assert gw.xstat("natgw_punt_finrst") >= 1
    assert gw.xstat("natgw_flows") == 0


def test_tcp_handshake_never_offloaded_early(fresh):
    """a connection that never completes its handshake is not offloaded"""
    gw = fresh
    # nothing listens on 6001: the server's RST closes it during the handshake
    gw.lan.run("python3", "-c", f"import socket; s=socket.socket(); s.settimeout(2)\n"
               f"try: s.connect(('{SERVER}', 6001))\nexcept OSError: pass", check=False)
    time.sleep(PAUSE)
    assert gw.offload()["sessions offloaded"] == 0


def test_many_udp_flows(fresh):
    gw = fresh
    seen = {}

    def between(phase):
        wait_for(lambda: gw.offloaded() == 32, what="32 sessions offloaded")
        seen["rx"] = gw.lan_rx()

    r = gw.client_phased(udp_args(flows=32, count=4, phases=2), between)
    assert r["received"] == r["sent"]
    assert gw.lan_rx() - seen["rx"] <= 2
    assert {p["peer"] for p in r["peers"]} == {WAN1_ADDR}
    assert gw.xstat("natgw_flows") == 64


def test_idle_session_expires_and_flows_removed(fresh):
    gw = fresh
    gw.cli("set nat timeout udp 2")
    r = gw.client(*udp_args(count=3, phases=2, gap=0.0))
    assert r["received"] == 6
    wait_for(lambda: gw.offloaded() == 1, what="offloaded")
    # hardware sees no traffic: after the timeout the sync deletes the session
    wait_for(lambda: gw.offload()["sessions expired after sync"] == 1, timeout=8, what="expiry")
    wait_for(lambda: gw.offloaded() == 0, what="flows removed")
    assert gw.sessions() == []
    assert gw.xstat("natgw_flows") == 0


def test_hardware_traffic_keeps_session_alive(fresh):
    """traffic only the hardware sees must keep VPP from expiring the session"""
    gw = fresh
    gw.cli("set nat timeout udp 2")
    ports = {}

    def between(phase):
        wait_for(lambda: gw.offloaded() == 1, what="offloaded")
        ports["before"] = [(s["in_port"], s["out_port"]) for s in gw.sessions()]

    # 5 s of traffic, 2.5x the timeout, all in hardware after phase 1
    r = gw.client_phased(["udp-client", "--dst", SERVER, "--port", "5000", "--count", "100", "--phases", "2",
                          "--gap", "0.025"], between)
    assert r["received"] == r["sent"] == 200
    c = gw.offload()
    assert c["sessions expired after sync"] == 0
    assert c["sessions offloaded"] == 1, "session was recreated: hardware traffic did not refresh it"
    assert [(s["in_port"], s["out_port"]) for s in gw.sessions()] == ports["before"]


def test_nat_session_delete_removes_flows(fresh):
    gw = fresh
    r = gw.client(*udp_args(count=4, phases=1, gap=0.01))
    wait_for(lambda: gw.offloaded() == 1, what="offloaded")
    port = r["local_ports"][0]
    gw.cli(f"nat44 del session in {LAN_HOST}:{port} udp external-host {SERVER}:5000")
    wait_for(lambda: gw.offloaded() == 0, what="flows removed")
    assert gw.offload()["sessions removed"] == 1
    assert gw.xstat("natgw_flows") == 0


def test_clear_sessions_removes_all_flows(fresh):
    gw = fresh
    gw.client(*udp_args(flows=8, count=4, phases=1))
    wait_for(lambda: gw.offloaded() == 8, what="offloaded")
    gw.cli("clear nat44 ed sessions")
    wait_for(lambda: gw.offloaded() == 0, what="flows removed")
    assert gw.xstat("natgw_flows") == 0


def test_disable_removes_flows_traffic_continues(fresh):
    gw = fresh

    def between(phase):
        wait_for(lambda: gw.offloaded() == 1, what="offloaded")
        gw.cli("natgw offload disable")
        assert gw.xstat("natgw_flows") == 0
        between.rx = gw.lan_rx()

    r = gw.client_phased(udp_args(count=5, phases=2), between)
    assert r["received"] == r["sent"]
    assert gw.lan_rx() - between.rx >= 5, "after disable VPP must forward"
    assert gw.offloaded() == 0


def test_neighbour_change_reinstalls(fresh):
    """the WAN router's MAC changes: flows are rewritten with the new MAC"""
    gw = fresh
    new_mac = "02:00:00:00:77:01"

    def between(phase):
        wait_for(lambda: gw.offloaded() == 1, what="offloaded")
        old = gw.wan1.run("cat", "/sys/class/net/eth0/address").stdout.strip()
        between.old = old
        gw.wan1.run("ip", "link", "set", "eth0", "address", new_mac)
        # the MAC change flushes the router's ARP cache; refill it from its
        # WAN address (ICMP is never offloaded)
        gw.wan1.run("ping", "-c", "1", "-W", "2", WAN1_ADDR, check=False)
        gw.cli(f"set ip neighbor NatgwModel1 {WAN1_GW} {new_mac} static")
        wait_for(lambda: gw.offload()["sessions reinstalled after next-hop change"] == 1, what="reinstall")
        between.rx = gw.lan_rx()

    try:
        r = gw.client_phased(udp_args(count=5, phases=2), between)
        assert r["received"] == r["sent"], "traffic after the MAC change was lost"
        assert gw.lan_rx() - between.rx <= 1, "still offloaded after reinstall"
        assert new_mac in gw.cli("show natgw offload sessions")
    finally:
        gw.cli(f"set ip neighbor del NatgwModel1 {WAN1_GW} {new_mac}")
        gw.wan1.run("ip", "link", "set", "eth0", "address", between.old)
        gw.wan1.run("ping", "-c", "1", "-W", "2", WAN1_ADDR, check=False)


def test_ecmp_offload_follows_vpp_path_choice(fresh):
    """with two default paths each flow leaves where VPP's hash sends it, with
    that WAN's address; the hardware must pick the same WAN (a wrong choice
    would send a flow out of the other WAN, whose server cannot reply)"""
    gw = fresh
    gw.cli(f"ip route add 0.0.0.0/0 via {WAN2_GW} NatgwModel2")
    try:
        def between(phase):
            wait_for(lambda: gw.offloaded() == 24, what="24 sessions offloaded")
            between.rx = gw.lan_rx()

        r = gw.client_phased(udp_args(flows=24, count=4, phases=2), between)
        assert r["received"] == r["sent"], r
        assert gw.lan_rx() - between.rx <= 2
        peers = [p["peer"] for p in r["peers"]]
        assert set(peers) == {WAN1_ADDR, WAN2_ADDR}, peers
        assert gw.xstat("natgw_rx_forwarded", "NatgwModel1") > 0
        assert gw.xstat("natgw_rx_forwarded", "NatgwModel2") > 0
    finally:
        gw.cli(f"ip route del 0.0.0.0/0 via {WAN2_GW} NatgwModel2")


def test_show_and_stats(fresh):
    gw = fresh
    gw.client(*udp_args(flows=3, count=4, phases=1))
    wait_for(lambda: gw.offloaded() == 3, what="offloaded")
    out = gw.cli("show natgw offload sessions")
    assert out.count("udp 192.168.1.10:") == 3
    from vpp_papi.vpp_stats import VPPStats
    st = VPPStats(socketname=f"{gw.vpp.dir}/stats.sock")
    st.connect()
    try:
        assert st["/natgw/offloaded"] == 3 or st["/natgw/offloaded"][0] == 3
    finally:
        st.disconnect()


@pytest.fixture(scope="module")
def small():
    """a table of 32 entries (16 sessions)"""
    reason = vppenv.skip_reason()
    if reason:
        pytest.skip(reason)
    g = Gateway("b", bucket_w=2)
    yield g
    g.close()


def test_table_full_leaves_flows_in_software(small):
    gw = small
    gw.reset()

    def between(phase):
        wait_for(lambda: gw.offload()["table full"] > 0, what="table full")
        between.n = gw.offloaded()

    r = gw.client_phased(udp_args(flows=24, count=4, phases=2), between)
    assert r["received"] == r["sent"], "flows that did not fit must still work"
    assert 8 <= between.n <= 16, between.n
    assert gw.xstat("natgw_flows") == 2 * gw.offloaded()
