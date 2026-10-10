# SPDX-License-Identifier: BSD-3-Clause
"""VPP offload with the shim model's optional DDR tier: sessions beyond the
on-chip table go to DDR when a working DIMM is there (all offloaded), and
stay in software (table full) when the tier is present but the memory did
not calibrate. DDR flows have no counters: their sessions are kept alive by
the activity bitmap through the AGE query."""

import pytest

from conftest import WAN1_ADDR, Gateway, vppenv, wait_for
from test_offload import udp_args


def _gateway(tag, vdev_args):
    reason = vppenv.skip_reason()
    if reason:
        pytest.skip(reason)
    return Gateway(tag, bucket_w=2, vdev_args=vdev_args)


@pytest.fixture(scope="module")
def ddr():
    """32 entries on chip (16 sessions), 1024 in DDR"""
    g = _gateway("d", ",ddr_bucket_w=8")
    yield g
    g.close()


@pytest.fixture(scope="module")
def no_dimm():
    """the same, with the DDR tier's memory not calibrated"""
    g = _gateway("e", ",ddr_bucket_w=8,ddr_calib=0")
    yield g
    g.close()


def test_sessions_beyond_on_chip_go_to_ddr(ddr):
    gw = ddr
    gw.reset()
    seen = {}

    def between(phase):
        wait_for(lambda: gw.offloaded() == 40, what="40 sessions offloaded")
        seen["rx"] = gw.lan_rx()

    r = gw.client_phased(udp_args(flows=40, count=4, phases=2), between)
    assert r["received"] == r["sent"]
    assert {p["peer"] for p in r["peers"]} == {WAN1_ADDR}
    assert gw.offload()["table full"] == 0
    assert gw.xstat("natgw_flows") == 80
    assert gw.xstat("natgw_ddr_flows") >= 80 - 32
    # phase 2 entirely in hardware, DDR flows included
    assert gw.lan_rx() - seen["rx"] <= 2
    assert gw.xstat("natgw_ddr_hits") > 0


def test_ddr_traffic_keeps_sessions_alive(ddr):
    """hardware-only traffic on DDR flows (no counters) must refresh VPP's
    sessions through the activity bitmap, and their idleness expire them"""
    gw = ddr
    gw.reset()
    gw.cli("set nat timeout udp 2")
    ports = {}

    def between(phase):
        wait_for(lambda: gw.offloaded() == 24, what="offloaded")
        ports["before"] = sorted((s["in_port"], s["out_port"]) for s in gw.sessions())

    # flows round robin, each sent to every ~0.5 s for ~5 s (2.5x the
    # timeout), all in hardware after phase 1
    r = gw.client_phased(udp_args(flows=24, count=10, phases=2, gap=0.02) + ["--interleave"], between)
    assert r["received"] == r["sent"]
    assert gw.xstat("natgw_ddr_flows") > 0
    c = gw.offload()
    assert c["sessions expired after sync"] == 0
    assert c["sessions offloaded"] == 24, "sessions were recreated: hardware traffic did not refresh them"
    assert sorted((s["in_port"], s["out_port"]) for s in gw.sessions()) == ports["before"]
    # then idle: all expire, on chip and DDR alike
    wait_for(lambda: gw.offload()["sessions expired after sync"] == 24, timeout=10, what="expiry")
    wait_for(lambda: gw.offloaded() == 0, what="flows removed")
    assert gw.xstat("natgw_flows") == 0 and gw.xstat("natgw_ddr_flows") == 0


def test_no_dimm_leaves_extra_sessions_in_software(no_dimm):
    gw = no_dimm
    gw.reset()

    def between(phase):
        wait_for(lambda: gw.offload()["table full"] > 0, what="table full")
        between.n = gw.offloaded()

    r = gw.client_phased(udp_args(flows=24, count=4, phases=2), between)
    assert r["received"] == r["sent"], "flows that did not fit must still work"
    assert 8 <= between.n <= 16, between.n
    assert gw.xstat("natgw_ddr_flows") == 0
    assert gw.xstat("natgw_ddr_lookups") == 0
