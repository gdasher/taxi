# SPDX-License-Identifier: BSD-3-Clause
"""libnatgw device API against the software model: register semantics,
per-entry state, events, aging and statistics."""

import random

import ctypes as C

import frames
from natgw_c import (OUT_FWD, OUT_PUNT, Entry, Event, Model, NextHop, Punt, State, Table, entry_c, key_c,
                     lib, nh_c, pm)

NATGW_REG_CTRL = 0x10


def dev(m):
    return C.byref(m.dev)


def test_init_and_reset_values():
    m = Model(6)
    assert m.dev.idx_w == 9 and m.dev.bucket_w == 6 and m.dev.lanes == 8 and m.dev.punt_hdr_len == 16
    assert lib.natgw_model_rd(m.h, NATGW_REG_CTRL) == 0x00ffff00     # disabled, all lanes bypassed
    lib.natgw_dev_set_ctrl(dev(m), True, True, 0x01, 0x7f)
    assert lib.natgw_model_rd(m.h, NATGW_REG_CTRL) == 0x007f0103
    assert lib.natgw_dev_clear(dev(m), 10) == 0
    m.close()


def test_init_rejects_wrong_device():
    def rd(ctx, off):
        return 0xdeadbeef

    def wr(ctx, off, val):
        pass

    from natgw_c import Dev, Io, RD, WR
    rdf, wrf = RD(rd), WR(wr)
    io = Io(rdf, wrf, None)
    d = Dev()
    assert lib.natgw_dev_init(C.byref(d), C.byref(io)) < 0


def test_entry_and_next_hop_roundtrip():
    random.seed(1)
    m = Model(5)
    for _ in range(50):
        e = pm.Entry(key=pm.Key(**frames.random_key(vid=random.randrange(4096))), xlate_dst=random.randint(0, 1),
                     new_ip=random.getrandbits(32), new_port=random.getrandbits(16),
                     dec_ttl=random.randint(0, 1), nh_idx=random.randrange(1024))
        idx = random.randrange(256)
        lib.natgw_dev_write_entry(dev(m), idx, C.byref(entry_c(e)))
        back = Entry()
        lib.natgw_dev_read_entry(dev(m), idx, C.byref(back))
        assert bytes(back) == bytes(entry_c(e))
        st = State()
        lib.natgw_dev_read_state(dev(m), idx, C.byref(st))
        assert st.valid == 1 and st.tcp == e.key.tcp and st.pkts == 0 and st.bytes == 0
        lib.natgw_dev_clear_entry(dev(m), idx)
        lib.natgw_dev_read_entry(dev(m), idx, C.byref(back))
        assert back.valid == 0
    nh = pm.NextHop(dst_mac=0x0200000000aa, src_mac=0x0200000000bb, lane=3, vlan=1, vid=42)
    lib.natgw_dev_write_nh(dev(m), 77, C.byref(nh_c(nh)))
    back = NextHop()
    lib.natgw_dev_read_nh(dev(m), 77, C.byref(back))
    assert bytes(back) == bytes(nh_c(nh))
    m.close()


def install_flow(m, t, key, nh_idx=1, xlate_dst=0):
    e = pm.Entry(key=key, xlate_dst=xlate_dst, new_ip=0xc6336401, new_port=4000, dec_ttl=1, nh_idx=nh_idx)
    n, idx, _ = t.insert(entry_c(e))
    lib.natgw_dev_apply(dev(m), t.ops, n)
    return idx


def setup(bucket_w=5):
    m = Model(bucket_w)
    lib.natgw_dev_set_ctrl(dev(m), True, True, 0, 0xff)
    nh = pm.NextHop(dst_mac=0x020000002200, src_mac=0x02000000aa01, lane=1)
    lib.natgw_dev_write_nh(dev(m), 1, C.byref(nh_c(nh)))
    return m, Table(bucket_w)


def test_hits_update_state_and_fin_rst_events():
    random.seed(2)
    m, t = setup()
    k = pm.Key(lane=0, vid=0, tcp=1, sip=0xc0a8010a, dip=0x08080808, sport=40000, dport=443)
    idx = install_flow(m, t, k)
    lib.natgw_model_advance(m.h, 5)
    total = 0
    for _ in range(7):
        f = frames.flow(k, flags="A")
        kind, lane, reason, hit, _ = m.rx(0, f)
        assert kind == OUT_FWD and lane == 1 and hit == idx
        total += len(f)
    st = State()
    lib.natgw_dev_read_state(dev(m), idx, C.byref(st))
    assert st.pkts == 7 and st.bytes == total and st.ts == 5 and not st.fin
    ev = Event()
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 0
    # FIN: punted with the hit flagged, state and event updated
    kind, lane, reason, hit, data = m.rx(0, frames.flow(k, flags="FA"))
    assert kind == OUT_PUNT and reason == pm.RSN_FINRST and hit == idx
    p = Punt()
    buf = (C.c_uint8 * 16).from_buffer_copy(data[:16])
    assert lib.natgw_punt_parse(buf, 16, C.byref(p)) == 0
    assert p.reason == pm.RSN_FINRST and p.idx == idx and p.flags & 0x08
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 1
    assert (ev.type, ev.idx, ev.tick) == (pm.EVT_FIN, idx, 5)
    m.rx(0, frames.flow(k, flags="R"))
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 1 and ev.type == pm.EVT_RST
    lib.natgw_dev_read_state(dev(m), idx, C.byref(st))
    assert st.fin and st.rst and st.pkts == 9
    m.close()
    t.close()


def test_idle_events_once_and_reset_by_traffic():
    random.seed(3)
    m, t = setup()
    lib.natgw_dev_set_thresholds(dev(m), 100, 10)
    udp = pm.Key(lane=0, vid=0, tcp=0, sip=1, dip=2, sport=3, dport=4)
    tcp = pm.Key(lane=0, vid=0, tcp=1, sip=1, dip=2, sport=3, dport=5)
    iu = install_flow(m, t, udp)
    it = install_flow(m, t, tcp)
    ev = Event()
    m.advance(10)
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 0       # not beyond the threshold yet
    m.advance(1)
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 1 and (ev.type, ev.idx) == (pm.EVT_IDLE, iu)
    m.advance(50)
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 0       # reported once
    m.rx(0, frames.flow(udp))                                        # traffic clears the pending flag
    m.advance(11)
    assert lib.natgw_dev_pop_event(dev(m), C.byref(ev)) == 1 and ev.idx == iu
    m.advance(100)
    got = set()
    while lib.natgw_dev_pop_event(dev(m), C.byref(ev)):
        got.add(ev.idx)
    assert got == {it}
    m.close()
    t.close()


def test_event_overflow_marker():
    m, t = setup(bucket_w=8)
    keys = []
    for i in range(1030):
        k = pm.Key(lane=0, vid=0, tcp=1, sip=i, dip=2, sport=3, dport=4)
        keys.append((k, install_flow(m, t, k)))
    for k, _ in keys:
        m.rx(0, frames.flow(k, flags="FA"))
    assert lib.natgw_dev_event_drops(dev(m)) == 6
    ev = Event()
    types = []
    while lib.natgw_dev_pop_event(dev(m), C.byref(ev)):
        types.append(ev.type)
    assert types.count(pm.EVT_FIN) == 1024 and types.count(pm.EVT_OVF) == 1 and types[-1] == pm.EVT_OVF
    m.close()
    t.close()


def test_clear_resets_tables():
    m, t = setup()
    k = pm.Key(lane=0, vid=0, tcp=0, sip=1, dip=2, sport=3, dport=4)
    idx = install_flow(m, t, k)
    assert lib.natgw_dev_clear(dev(m), 10) == 0
    e = Entry()
    lib.natgw_dev_read_entry(dev(m), idx, C.byref(e))
    nh = NextHop()
    lib.natgw_dev_read_nh(dev(m), 1, C.byref(nh))
    assert e.valid == 0 and nh.valid == 0
    kind, _, reason, _, _ = m.rx(0, frames.flow(k))
    assert kind == OUT_PUNT and reason == pm.RSN_MISS
    m.close()
    t.close()
