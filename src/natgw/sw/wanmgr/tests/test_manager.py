# SPDX-License-Identifier: BSD-3-Clause
"""WanManager against FakeVpp: steering, NAT pool, probes, leases, restarts."""

import json

import pytest

from fakes import FakeClock, FakeVpp, lease
from wanmgr import config
from wanmgr.health import State
from wanmgr.manager import DEFAULT, VppDisconnected, WanManager

CFG = """
[probe]
interval = 2.0
timeout = 0.5
up_rounds = 5
down_rounds = 3
initial_rounds = 1

[[wan]]
name = "wan1"
interface = "WAN1"
probe_targets = ["192.0.2.1", "192.0.2.2"]

[[wan]]
name = "wan2"
interface = "WAN2"
probe_targets = ["192.0.2.11", "192.0.2.12"]
weight = 3
"""

W1, W2 = 1, 2
GW1, GW2 = "198.51.100.2", "198.18.0.2"
A1, A2 = "198.51.100.1", "198.18.0.1"


class Alerts:
    def __init__(self):
        self.subjects = []
        self.bodies = []

    def alert(self, subject, body):
        self.subjects.append(subject)
        self.bodies.append(body)

    def flush(self):
        pass


@pytest.fixture
def env():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2, "LAN": 3},
                  {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    clock = FakeClock()
    alerts = Alerts()
    mgr = WanManager(config.parse(CFG), vpp, alerts, clock=clock, sleep=clock.sleep)
    mgr.connect()
    return mgr, vpp, alerts


def rounds(mgr, n):
    for _ in range(n):
        mgr.round()


def default(vpp):
    return vpp.routes.get(DEFAULT)


def both():
    return [(W1, GW1, 1), (W2, GW2, 3)]


def test_startup_steers_over_both_wans(env):
    mgr, vpp, alerts = env
    rounds(mgr, 1)
    assert [w.state for w in mgr.wans] == [State.UP, State.UP]
    assert default(vpp) == both()
    assert vpp.pool == {A1, A2}
    # every probe target is pinned to its own WAN
    for t in ("192.0.2.1", "192.0.2.2"):
        assert vpp.routes[f"{t}/32"] == [(W1, GW1, 1)]
    for t in ("192.0.2.11", "192.0.2.12"):
        assert vpp.routes[f"{t}/32"] == [(W2, GW2, 1)]
    assert alerts.subjects == []
    # healthy rounds probe one target per WAN
    vpp.pings.clear()
    rounds(mgr, 1)
    assert vpp.pings == ["192.0.2.1", "192.0.2.11"]


def test_wan_fails_after_three_rounds_and_is_flushed(env):
    mgr, vpp, alerts = env
    rounds(mgr, 1)
    vpp.alive_gws = {GW2}
    rounds(mgr, 2)
    assert default(vpp) == both(), "two failed rounds must not steer"
    assert vpp.flushed == []
    rounds(mgr, 1)
    assert mgr.wans[0].state == State.DOWN
    assert default(vpp) == [(W2, GW2, 3)]
    # sessions flushed (address removed and re-added), after the route moved
    assert vpp.pool == {A1, A2} and vpp.flushed == [A1]
    tail = vpp.changes()[-3:]
    assert tail == [("route_set", DEFAULT, ((W2, GW2, 3),)), ("pool_del", A1, 0), ("pool_add", A1, 0)]
    assert alerts.subjects == ["wan1 DOWN"]
    assert "Healthy WANs: wan2" in alerts.bodies[0]
    # the route is replaced as a whole set, never path by path
    assert all(e[0] == "route_set" for e in vpp.changes() if e[1] == DEFAULT)


def test_recovery_needs_five_good_rounds(env):
    mgr, vpp, alerts = env
    rounds(mgr, 1)
    vpp.alive_gws = {GW2}
    rounds(mgr, 3)
    vpp.alive_gws = {GW1, GW2}
    rounds(mgr, 4)
    assert mgr.wans[0].state == State.DOWN and default(vpp) == [(W2, GW2, 3)]
    rounds(mgr, 1)
    assert mgr.wans[0].state == State.UP
    assert default(vpp) == both() and vpp.pool == {A1, A2}
    assert alerts.subjects == ["wan1 DOWN", "wan1 UP"]


def test_flapping_wan_never_goes_down(env):
    mgr, vpp, _ = env
    rounds(mgr, 1)
    for i in range(20):
        vpp.alive_gws = {GW2} if i % 3 != 2 else {GW1, GW2}   # two bad, one good
        rounds(mgr, 1)
    assert mgr.wans[0].state == State.UP
    assert vpp.flushed == []


def test_second_probe_target_keeps_wan_up(env):
    mgr, vpp, _ = env
    vpp.reachable[GW1] = {"192.0.2.2"}      # the first target is dead
    rounds(mgr, 6)
    assert mgr.wans[0].state == State.UP
    assert mgr.wans[0].last_probe == "192.0.2.2"


def test_all_down_removes_override_and_keeps_all_addresses(env):
    mgr, vpp, alerts = env
    rounds(mgr, 1)
    vpp.alive_gws = set()
    rounds(mgr, 3)
    assert [w.state for w in mgr.wans] == [State.DOWN, State.DOWN]
    assert default(vpp) is None and ("route_del", DEFAULT) in vpp.changes()
    # DHCP's own routes now use every WAN: their addresses stay usable
    assert vpp.pool == {A1, A2} and vpp.flushed == []
    assert "No WAN is healthy" in alerts.bodies[-1]
    # one recovers: steer to it alone; only now are the other's sessions
    # moved (there was nowhere better before)
    vpp.alive_gws = {GW2}
    rounds(mgr, 4)
    assert vpp.flushed == []
    rounds(mgr, 1)
    assert default(vpp) == [(W2, GW2, 3)]
    assert vpp.pool == {A1, A2} and vpp.flushed == [A1]


def test_probes_go_out_of_their_own_wan(env):
    """WAN1's gateway is dead but WAN2 could reach WAN1's targets: the pinned
    /32 routes keep WAN1's probes on WAN1"""
    mgr, vpp, _ = env
    rounds(mgr, 1)
    vpp.alive_gws = {GW2}
    rounds(mgr, 3)
    assert mgr.wans[0].state == State.DOWN


def test_lease_change_flushes_old_address_first(env):
    mgr, vpp, alerts = env
    rounds(mgr, 2)
    vpp.log.clear()
    vpp.clients[W1] = lease("198.51.100.77", "198.51.100.254")
    vpp.alive_gws = {"198.51.100.254", GW2}
    rounds(mgr, 1)
    pool = [e for e in vpp.changes() if e[0].startswith("pool")]
    assert pool == [("pool_del", A1, 0), ("pool_add", "198.51.100.77", 0)]
    assert default(vpp) == [(W1, "198.51.100.254", 1), (W2, GW2, 3)]
    assert vpp.routes["192.0.2.1/32"] == [(W1, "198.51.100.254", 1)]
    assert alerts.subjects == ["wan1 lease changed"]


def test_lease_lost_is_down_at_once(env):
    mgr, vpp, alerts = env
    rounds(mgr, 1)
    vpp.clients[W1] = None
    rounds(mgr, 1)
    assert mgr.wans[0].state == State.DOWN
    assert default(vpp) == [(W2, GW2, 3)]
    assert vpp.flushed == [A1]
    assert "192.0.2.1/32" not in vpp.routes and "192.0.2.2/32" not in vpp.routes
    assert alerts.subjects == ["wan1 lease lost", "wan1 DOWN"]


def test_startup_with_dead_wan_waits_for_a_verdict(env):
    mgr, vpp, alerts = env
    vpp.alive_gws = {GW2}
    rounds(mgr, 2)
    # WAN1 is still unknown: nothing is steered yet (the pool is needed to probe)
    assert default(vpp) is None and vpp.pool == {A1, A2}
    rounds(mgr, 1)
    assert default(vpp) == [(W2, GW2, 3)]
    assert vpp.flushed == [A1]
    assert alerts.subjects == ["wan1 DOWN"]


def test_vpp_restart_reconnects_and_reapplies_after_probing():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    clock = FakeClock()
    alerts = Alerts()
    mgr = WanManager(config.parse(CFG), vpp, alerts, clock=clock, sleep=clock.sleep)
    script = []

    def stop():
        n = mgr.rounds
        if n == 3 and not script:
            script.append("restart")
            vpp.restart()          # routes, pool and leases are gone
        if n == 5 and len(script) == 1:
            script.append("rebind")
            vpp.clients = {W1: lease(A1, GW1), W2: lease(A2, GW2)}
        return n >= 8

    mgr.run(stop=stop)
    assert script == ["restart", "rebind"]
    assert default(vpp) == both()
    assert vpp.pool == {A1, A2}
    assert vpp.routes["192.0.2.11/32"] == [(W2, GW2, 1)]
    assert "VPP connection lost" in alerts.subjects and "VPP reconnected" in alerts.subjects
    # leases relearned quietly after the restart: no lease-lost alerts
    assert not any("lease lost" in s for s in alerts.subjects)


def test_vpp_down_retries_with_backoff():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    vpp.down = True
    clock = FakeClock()
    mgr = WanManager(config.parse(CFG), vpp, Alerts(), clock=clock, sleep=clock.sleep)
    t0 = clock()
    mgr.run(stop=lambda: clock() - t0 > 10)
    assert not mgr.connected and mgr.rounds == 0
    vpp.down = False
    mgr.run(stop=lambda: mgr.rounds >= 1)
    assert default(vpp) == both()


def test_daemon_restart_adopts_existing_state(env):
    _, vpp, _ = env
    env[0].round()
    vpp.log.clear()
    clock = FakeClock()
    mgr2 = WanManager(config.parse(CFG), vpp, Alerts(), clock=clock, sleep=clock.sleep)
    mgr2.connect()
    rounds(mgr2, 1)
    # same route set again (it may have gone stale), no pool churn: sessions survive
    assert [e[0] for e in vpp.changes() if e[0].startswith("pool")] == []
    assert vpp.flushed == []
    assert default(vpp) == both()


def test_dhcp_clients_created_when_missing():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W2: None})
    mgr = WanManager(config.parse(CFG), vpp, Alerts())
    mgr.connect()
    assert vpp.changes("dhcp_client") == [("dhcp_client", W1, "natgw")]
    cfg = config.parse(CFG.replace('probe_targets = ["192.0.2.1", "192.0.2.2"]',
                                   'probe_targets = ["192.0.2.1", "192.0.2.2"]\nmanage_dhcp = false'))
    vpp2 = FakeVpp({"WAN1": W1, "WAN2": W2}, {W2: None})
    WanManager(cfg, vpp2, Alerts()).connect()
    assert vpp2.changes("dhcp_client") == []


def test_missing_interface_is_a_wan_without_lease():
    vpp = FakeVpp({"WAN2": W2}, {W2: lease(A2, GW2)})
    vpp.alive_gws = {GW2}
    clock = FakeClock()
    mgr = WanManager(config.parse(CFG), vpp, Alerts(), clock=clock, sleep=clock.sleep)
    mgr.connect()
    rounds(mgr, 1)
    # no lease yet, within the DHCP grace: not judged, not in the way
    assert mgr.wans[0].state == State.UNKNOWN
    assert default(vpp) == [(W2, GW2, 3)]
    clock.t += 31
    rounds(mgr, 1)
    assert mgr.wans[0].state == State.DOWN


def test_disconnect_mid_round_is_recovered():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    clock = FakeClock()
    alerts = Alerts()
    mgr = WanManager(config.parse(CFG), vpp, alerts, clock=clock, sleep=clock.sleep)
    mgr.connect()
    rounds(mgr, 1)
    vpp.fail_after = 1
    with pytest.raises(VppDisconnected):
        mgr.round()
    mgr.run(stop=lambda: mgr.rounds >= 4)
    assert default(vpp) == both() and mgr.connected


def test_status_file(env, tmp_path):
    mgr, vpp, _ = env
    mgr.status_file = str(tmp_path / "status.json")
    rounds(mgr, 1)
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["wans"]["wan1"]["state"] == "up"
    assert st["wans"]["wan2"]["lease"]["address"] == A2
    assert st["nat_pool"] == sorted([A1, A2])
    assert len(st["route"]) == 2


def test_round_timing():
    """rounds start every interval, independent of time spent probing"""
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    clock = FakeClock()
    starts = []
    mgr = WanManager(config.parse(CFG), vpp, Alerts(), clock=clock, sleep=clock.sleep)
    orig = mgr.round

    def timed():
        starts.append(clock())
        clock.t += 0.7      # probing takes time
        orig()

    mgr.round = timed
    mgr.run(stop=lambda: len(starts) >= 4)
    assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [2.0, 2.0, 2.0]


def test_api_error_does_not_stop_the_loop():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    clock = FakeClock()
    mgr = WanManager(config.parse(CFG), vpp, Alerts(), clock=clock, sleep=clock.sleep)
    real_add, calls = vpp.nat_pool_add, []

    def flaky_add(address, vrf_id):
        calls.append(address)
        if len(calls) == 1:
            raise RuntimeError("nat44 plugin not enabled yet")
        real_add(address, vrf_id)

    vpp.nat_pool_add = flaky_add
    mgr.run(stop=lambda: mgr.rounds >= 3)
    assert vpp.pool == {A1, A2}


def test_down_wan_can_be_probed_back_up(env):
    """its address must stay in the pool: VPP's probes go through NAT"""
    mgr, vpp, _ = env
    rounds(mgr, 1)
    vpp.alive_gws = {GW2}
    rounds(mgr, 3)
    assert A1 in vpp.pool
    vpp.alive_gws = {GW1, GW2}
    rounds(mgr, 5)
    assert mgr.wans[0].state == State.UP


def test_startup_without_leases_is_quiet():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: None, W2: None})
    vpp.alive_gws = {GW1, GW2}
    clock = FakeClock()
    alerts = Alerts()
    mgr = WanManager(config.parse(CFG), vpp, alerts, clock=clock, sleep=clock.sleep)
    mgr.connect()
    rounds(mgr, 3)
    vpp.clients = {W1: lease(A1, GW1), W2: lease(A2, GW2)}
    rounds(mgr, 2)
    assert default(vpp) == both()
    assert alerts.subjects == []


def test_lease_lost_then_back_alerts_both():
    vpp = FakeVpp({"WAN1": W1, "WAN2": W2}, {W1: lease(A1, GW1), W2: lease(A2, GW2)})
    vpp.alive_gws = {GW1, GW2}
    alerts = Alerts()
    mgr = WanManager(config.parse(CFG), vpp, alerts)
    mgr.connect()
    rounds(mgr, 1)
    vpp.clients[W1] = None
    rounds(mgr, 1)
    vpp.clients[W1] = lease(A1, GW1)
    rounds(mgr, 1)
    assert alerts.subjects == ["wan1 lease lost", "wan1 DOWN", "wan1 lease acquired"]
    assert mgr.wans[0].state == State.UP      # a new lease gets a fresh, quick verdict
