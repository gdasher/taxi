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
from test_model import check_forward, make_world, traffic

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


def place(m, entries, nhs):
    """Program on chip first, the DDR tier once the on-chip table refuses."""
    for idx, nh in nhs.items():
        m.nh[idx] = nh
    where = {}
    for e in entries:
        try:
            m.table.insert(e)
            where[e.key] = "uram"
        except pm.TableFull:
            m.ddr.insert(e)
            where[e.key] = "ddr"
    return where


@pytest.mark.parametrize("seed", [1, 2])
def test_tiers(seed):
    """Flows in both tiers forward exactly as their entries say (checked with
    scapy); a DDR hit reports the DDR index the table placed it at, flagged;
    the activity bitmap holds exactly the DDR entries that hit."""
    random.seed(seed)
    m = pm.ShimModel(bucket_w=3, enable=True, bypass_mask=0, punt_hdr=True, ddr_bucket_w=7)
    m.ddr_active = True
    entries, nhs = make_world(200)
    where = place(m, entries, nhs)
    assert 40 < sum(v == "ddr" for v in where.values()) < 200
    ddr_fwd, ddr_hit = 0, set()
    for lane, frame in traffic(entries, 3000):
        out = m.process(lane, frame)
        key = pm.parse(frame, lane).key
        tier = where.get(key)
        if out.hit_idx is not None:
            if tier == "ddr":
                assert out.hit_idx == pm.DDR_IDX_FLAG | m.ddr.where[key]
                ddr_hit.add(m.ddr.where[key])
            else:
                assert tier == "uram" and out.hit_idx == m.table.where[key]
        if out.kind == "fwd":
            e = m.ddr.slots[out.hit_idx & ~pm.DDR_IDX_FLAG] if tier == "ddr" else m.table.slots[out.hit_idx]
            check_forward(m, lane, frame, out, e)
            ddr_fwd += tier == "ddr"
    assert ddr_fwd > 100
    words = m.ddr.size // 64
    got = set()
    for w in range(words):
        v = m.read_activity(w)
        got |= {w * 64 + b for b in range(64) if (v >> b) & 1}
    assert got == ddr_hit and got
    assert all(m.read_activity(w) == 0 for w in range(words))
    lookups, hits, skips = C.c_uint32(), C.c_uint32(), C.c_uint32()
    lib.natgw_dev_ddr_stats(C.byref(m.dev), C.byref(lookups), C.byref(hits), C.byref(skips))
    assert hits.value >= ddr_fwd and lookups.value > hits.value and skips.value == 0
    assert lib.natgw_dev_ddr_read_errors(C.byref(m.dev)) == 0


def test_disabled_tier_not_looked_up():
    """Entries in DDR but the tier disabled: they miss, no activity."""
    random.seed(3)
    m = pm.ShimModel(bucket_w=3, enable=True, bypass_mask=0, punt_hdr=True, ddr_bucket_w=6)
    entries, nhs = make_world(100)
    where = place(m, entries, nhs)
    for lane, frame in traffic(entries, 500):
        out = m.process(lane, frame)
        if where.get(pm.parse(frame, lane).key) == "ddr":
            assert out.hit_idx is None and out.kind == "punt"
    assert all(m.read_activity(w) == 0 for w in range(4))
