# SPDX-License-Identifier: BSD-3-Clause
"""The optional DDR tier through libnatgw and the C model: status when absent,
present without a DIMM, and working; entry access, clearing, the activity
bitmap; and packet behaviour cross-checked against the Python model with
flows split between the on-chip and DDR tiers."""

import random

import ctypes as C
import pytest

import frames
from natgw_c import OUT_FWD, OUT_PUNT, DdrStatus, Entry, Model, Table, entry_c, key_c, lib, pm
from test_model import compare, make_world, setup_pair, traffic

NATGW_REG_DDR_STATUS = 0x60


def dev(m):
    return C.byref(m.dev)


def status(m):
    st = DdrStatus()
    lib.natgw_dev_ddr_status(dev(m), C.byref(st))
    return st


def rand_entry():
    return pm.Entry(key=pm.Key(**frames.random_key()), xlate_dst=random.randint(0, 1),
                    new_ip=random.getrandbits(32), new_port=random.getrandbits(16),
                    dec_ttl=1, nh_idx=random.randrange(1, 1024))


def test_absent():
    """No DDR tier in the build: every status bit zero, commands ignored."""
    m = Model(5)
    assert lib.natgw_model_rd(m.h, NATGW_REG_DDR_STATUS) == 0
    st = status(m)
    assert not (st.present or st.calibrated or st.enabled or st.active) and st.bucket_w == 0
    lib.natgw_dev_ddr_enable(dev(m), True)
    assert not status(m).active
    e = entry_c(rand_entry())
    lib.natgw_dev_write_ddr_entry(dev(m), 3, C.byref(e))
    assert lib.natgw_dev_read_activity(dev(m), 0) == 0
    m.close()


def test_present_without_dimm():
    """DDR tier built in, memory not calibrated: never active."""
    m = Model(5, ddr_bucket_w=6, ddr_calib=False)
    st = status(m)
    assert st.present and not st.calibrated and st.bucket_w == 6 and st.max_out > 0
    lib.natgw_dev_ddr_enable(dev(m), True)
    st = status(m)
    assert st.enabled and not st.active
    m.close()


def test_clear_enable_and_entries():
    random.seed(1)
    m = Model(5, ddr_bucket_w=6)
    st = status(m)
    assert st.present and st.calibrated and not st.enabled and not st.active
    # contents are junk until cleared
    junk = 0
    for i in range(64):
        e = Entry()
        lib.natgw_dev_read_ddr_entry(dev(m), i, C.byref(e))
        junk += bytes(e) != bytes(Entry())
    assert junk > 50
    assert lib.natgw_dev_ddr_clear(dev(m), 100) == 0
    for i in range(0, 256, 17):
        e = Entry()
        lib.natgw_dev_read_ddr_entry(dev(m), i, C.byref(e))
        assert bytes(e) == bytes(Entry())
    for w in range(4):
        assert lib.natgw_dev_read_activity(dev(m), w) == 0
    lib.natgw_dev_ddr_enable(dev(m), True)
    assert status(m).active
    for _ in range(40):
        i = random.randrange(256)
        e = entry_c(rand_entry())
        lib.natgw_dev_write_ddr_entry(dev(m), i, C.byref(e))
        back = Entry()
        lib.natgw_dev_read_ddr_entry(dev(m), i, C.byref(back))
        assert bytes(back) == bytes(e)
        lib.natgw_dev_clear_ddr_entry(dev(m), i)
        lib.natgw_dev_read_ddr_entry(dev(m), i, C.byref(back))
        assert back.valid == 0
    m.close()


def place(m, py, entries, nhs, uram_bw, ddr_bw):
    """Program on-chip first, the DDR tier once the on-chip table refuses."""
    for idx, nh in nhs.items():
        from natgw_c import nh_c
        py.nh[idx] = nh
        lib.natgw_dev_write_nh(dev(m), idx, C.byref(nh_c(nh)))
    t = Table(uram_bw)
    td = Table(ddr_bw, ddr=True)
    where = {}
    for e in entries:
        n, idx, _ = t.insert(entry_c(e))
        if n >= 1:
            lib.natgw_dev_apply(dev(m), t.ops, n)
            py.table.insert(e)
            where[e.key] = "uram"
            continue
        n, idx, _ = td.insert(entry_c(e))
        assert n >= 1
        lib.natgw_dev_apply_ddr(dev(m), td.ops, n)
        py.ddr.insert(e)
        where[e.key] = "ddr"
    return t, td, where


@pytest.mark.parametrize("seed", [1, 2])
def test_tiers_match_python(seed):
    random.seed(seed)
    uram_bw, ddr_bw = 3, 7
    m, py = setup_pair(uram_bw)
    assert lib.natgw_model_set_ddr(m.h, ddr_bw, True) == 0
    py.ddr_bucket_w = ddr_bw
    py.ddr = pm.DdrTable(ddr_bw, py.seed0, py.seed1)
    py.ddr_active = True
    assert lib.natgw_dev_ddr_clear(dev(m), 100) == 0
    lib.natgw_dev_ddr_enable(dev(m), True)
    entries, nhs = make_world(200)
    t, td, where = place(m, py, entries, nhs, uram_bw, ddr_bw)
    assert 40 < sum(v == "ddr" for v in where.values()) < 200
    ddr_fwd = 0
    for lane, frame in traffic(entries, 3000):
        kind, olane, reason, hit_idx, data = m.rx(lane, frame)
        exp = py.process(lane, frame)
        assert (kind, olane, reason) == (OUT_FWD if exp.kind == "fwd" else OUT_PUNT, exp.lane, exp.reason)
        assert data == exp.data
        assert hit_idx == (0xffffffff if exp.hit_idx is None else exp.hit_idx)
        ddr_fwd += kind == OUT_FWD and hit_idx & 0x80000000 != 0
    assert ddr_fwd > 100
    # activity bitmap: the same set of DDR entries, then all clear
    words = td.size // 64
    got = [lib.natgw_dev_read_activity(dev(m), w) for w in range(words)]
    exp = [py.read_activity(w) for w in range(words)]
    assert got == exp and any(got)
    assert all(lib.natgw_dev_read_activity(dev(m), w) == 0 for w in range(words))
    lookups, hits, skips = C.c_uint32(), C.c_uint32(), C.c_uint32()
    lib.natgw_dev_ddr_stats(dev(m), C.byref(lookups), C.byref(hits), C.byref(skips))
    assert hits.value >= ddr_fwd and lookups.value > hits.value and skips.value == 0
    for lane in range(8):
        for r in range(16):
            assert lib.natgw_dev_read_stat(dev(m), lane, r) == py.stats.get((lane, r), 0)
    m.close()


def test_disabled_tier_not_looked_up():
    """Entries in DDR but the tier disabled: misses, no activity."""
    random.seed(3)
    m, py = setup_pair(3)
    assert lib.natgw_model_set_ddr(m.h, 6, True) == 0
    assert lib.natgw_dev_ddr_clear(dev(m), 100) == 0
    entries, nhs = make_world(100)
    py.ddr = pm.DdrTable(6, py.seed0, py.seed1)
    py.ddr_active = False
    place(m, py, entries, nhs, 3, 6)
    for lane, frame in traffic(entries, 500):
        compare(m, py, lane, frame)
    assert all(lib.natgw_dev_read_activity(dev(m), w) == 0 for w in range(4))
    m.close()
