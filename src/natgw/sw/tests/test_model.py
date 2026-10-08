# SPDX-License-Identifier: BSD-3-Clause
"""The C software model against the Python model (the RTL scoreboard):
identical outputs, byte for byte, and identical statistics."""

import random

import ctypes as C
import pytest

import frames
from natgw_c import OUT_FWD, OUT_PUNT, Model, Table, entry_c, key_c, lib, nh_c, pm


def setup_pair(bucket_w, enable=True, punt_hdr=True, bypass=0, egress_en=0xff, seeds=(0xffffffff, 0xffffffff)):
    m = Model(bucket_w)
    lib.natgw_dev_set_seeds(C.byref(m.dev), *seeds)
    lib.natgw_dev_set_ctrl(C.byref(m.dev), enable, punt_hdr, bypass, egress_en)
    py = pm.ShimModel(bucket_w=bucket_w, seed0=seeds[0], seed1=seeds[1], enable=enable,
                      bypass_mask=bypass, punt_hdr=punt_hdr, egress_en=egress_en)
    return m, py


def program(m, py, entries, nhs, seeds=(0xffffffff, 0xffffffff)):
    t = Table(m.dev.bucket_w, *seeds)
    for idx, nh in nhs.items():
        py.nh[idx] = nh
        lib.natgw_dev_write_nh(C.byref(m.dev), idx, C.byref(nh_c(nh)))
    for e in entries:
        n, _, _ = t.insert(entry_c(e))
        assert n >= 1
        lib.natgw_dev_apply(C.byref(m.dev), t.ops, n)
        py.table.insert(e)
    return t


def make_world(nflows, vlan_frac=0.2):
    nhs = {}
    for i in range(1, 9):
        vlan = 1 if i > 6 else 0
        nhs[i] = pm.NextHop(dst_mac=0x020000002200 + i, src_mac=0x02000000aa00 + i, lane=i % 8,
                            vlan=vlan, vid=100 + i if vlan else 0, valid=0 if i == 5 else 1)
    entries = []
    for _ in range(nflows):
        vid = random.choice([10, 20]) if random.random() < vlan_frac else 0
        k = pm.Key(**frames.random_key(vid=vid))
        entries.append(pm.Entry(key=k, xlate_dst=random.randint(0, 1), new_ip=random.getrandbits(32),
                                new_port=random.getrandbits(16), dec_ttl=random.randint(0, 1),
                                nh_idx=random.randint(1, 8)))
    return entries, nhs


def traffic(entries, n):
    out = []
    for _ in range(n):
        r = random.random()
        if r < 0.55:
            e = random.choice(entries)
            flags = random.choice(["A", "A", "A", "PA", "FA", "R", "S"])
            ttl = random.choice([64, 64, 64, 2, 1])
            out.append((e.key.lane, frames.flow(e.key, flags=flags, ttl=ttl)))
        elif r < 0.75:
            k = pm.Key(**frames.random_key(vid=random.choice([0, 0, 10])))
            out.append((k.lane, frames.flow(k)))
        elif r < 0.92:
            out.append((random.randrange(8), random.choice(frames.exceptions())))
        else:
            out.append((random.randrange(8), frames.garbage()))
    return out


def compare(m, py, lane, frame):
    kind, olane, reason, hit_idx, data = m.rx(lane, frame)
    exp = py.process(lane, frame)
    assert reason == exp.reason, (exp, frame.hex())
    assert kind == (OUT_FWD if exp.kind == "fwd" else OUT_PUNT)
    assert olane == exp.lane
    assert data == exp.data, f"\n got {data.hex()}\n exp {exp.data.hex()}"
    return kind


@pytest.mark.parametrize("seed,punt_hdr,egress_en,seeds", [
    (1, True, 0xff, (0xffffffff, 0xffffffff)),
    (2, False, 0xff, (0xffffffff, 0xffffffff)),
    (3, True, 0xbf, (0x1234abcd, 0x5555aaaa)),
])
def test_model_matches_python(seed, punt_hdr, egress_en, seeds):
    random.seed(seed)
    m, py = setup_pair(7, punt_hdr=punt_hdr, egress_en=egress_en, seeds=seeds)
    entries, nhs = make_world(300)
    program(m, py, entries, nhs, seeds)
    kinds = {OUT_FWD: 0, OUT_PUNT: 0}
    for lane, frame in traffic(entries, 3000):
        kinds[compare(m, py, lane, frame)] += 1
    assert kinds[OUT_FWD] > 300 and kinds[OUT_PUNT] > 300
    for lane in range(8):
        for r in range(16):
            assert lib.natgw_dev_read_stat(C.byref(m.dev), lane, r) == py.stats.get((lane, r), 0), (lane, r)
    m.close()


def test_bypass_and_disabled():
    random.seed(9)
    for enable, bypass in ((False, 0), (True, 0x0f)):
        m, py = setup_pair(5, enable=enable, bypass=bypass)
        entries, nhs = make_world(50)
        program(m, py, entries, nhs)
        for lane, frame in traffic(entries, 400):
            compare(m, py, lane, frame)
        m.close()


def test_every_reason_reachable():
    """The traffic mix exercises every punt reason and forwarding."""
    random.seed(10)
    m, py = setup_pair(6)
    entries, nhs = make_world(100)
    program(m, py, entries, nhs)
    seen = set()
    for lane, frame in traffic(entries, 4000):
        compare(m, py, lane, frame)
    for (lane, r), n in py.stats.items():
        if n:
            seen.add(r)
    expected = {pm.RSN_MISS, pm.RSN_NOT_IPV4, pm.RSN_MCAST, pm.RSN_IP_HDR, pm.RSN_FRAG, pm.RSN_TTL,
                pm.RSN_PROTO, pm.RSN_CSUM, pm.RSN_SYN, pm.RSN_FINRST, pm.RSN_VLAN, pm.RSN_NH, pm.RSN_FWD}
    assert expected <= seen, expected - seen
    m.close()
