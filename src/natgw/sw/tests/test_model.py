# SPDX-License-Identifier: BSD-3-Clause
"""The C software model (the one behavioural reference, also the RTL
testbenches' scoreboard) against independent checks: forwarded frames are
decoded with scapy and compared field by field with the entry and next hop
that hit (addresses, ports, MACs, VLAN, TTL, and valid IP/TCP/UDP checksums);
frames that do not hit are punted unchanged; every punt reason is reachable;
bypass and disabled lanes punt everything unchanged."""

import random

import ctypes as C
import pytest
from scapy.layers.inet import IP, TCP, UDP
from scapy.layers.l2 import Dot1Q, Ether

import frames
from natgw_c import OUT_FWD, OUT_PUNT, lib, pm


def make_model(bucket_w, enable=True, punt_hdr=True, bypass=0, egress_en=0xff, seeds=(0xffffffff, 0xffffffff)):
    return pm.ShimModel(bucket_w=bucket_w, seed0=seeds[0], seed1=seeds[1], enable=enable, bypass_mask=bypass,
                        punt_hdr=punt_hdr, egress_en=egress_en)


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


def program(m, entries, nhs):
    for idx, nh in nhs.items():
        m.nh[idx] = nh
    for e in entries:
        m.table.insert(e)


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


def ip_int(s):
    return int.from_bytes(bytes(int(x) for x in s.split(".")), "big")


def check_forward(m, lane, frame, out, hit_entry):
    """a forwarded frame, decoded independently"""
    e = hit_entry
    nh = m.nh[e.nh_idx]
    assert out.lane == nh.lane and nh.valid and (m.egress_en >> nh.lane) & 1
    a, b = Ether(frame), Ether(out.data)
    assert len(out.data) == len(frame)
    assert int(b.dst.replace(":", ""), 16) == nh.dst_mac and int(b.src.replace(":", ""), 16) == nh.src_mac
    if Dot1Q in a:
        assert Dot1Q in b and b[Dot1Q].vlan == nh.vid and b[Dot1Q].prio == a[Dot1Q].prio
    else:
        assert Dot1Q not in b
    ia, ib = a[IP], b[IP]
    l4a, l4b = (a[TCP], b[TCP]) if TCP in a else (a[UDP], b[UDP])
    want_ip, want_port = e.new_ip, e.new_port
    if e.xlate_dst:
        assert (ip_int(ib.dst), l4b.dport, ib.src, l4b.sport) == (want_ip, want_port, ia.src, l4a.sport)
    else:
        assert (ip_int(ib.src), l4b.sport, ib.dst, l4b.dport) == (want_ip, want_port, ia.dst, l4a.dport)
    assert ib.ttl == ia.ttl - (1 if e.dec_ttl else 0)
    assert bytes(b)[len(bytes(b)) - len(bytes(l4b.payload)):] == bytes(l4a.payload)
    # checksums: recompute independently and compare with what the model wrote
    c = Ether(out.data)
    del c[IP].chksum
    if TCP in c:
        del c[TCP].chksum
        assert Ether(bytes(c))[TCP].chksum == b[TCP].chksum
    elif b[UDP].chksum:
        del c[UDP].chksum
        assert Ether(bytes(c))[UDP].chksum in (b[UDP].chksum, 0xffff if b[UDP].chksum == 0 else -1)
    assert Ether(bytes(c))[IP].chksum == b[IP].chksum


def run_traffic(m, entries, items):
    """returns the forward count; every output is checked"""
    fwd = 0
    for lane, frame in items:
        out = m.process(lane, frame)
        if out.kind == "fwd":
            fwd += 1
            idx = out.hit_idx
            assert idx is not None
            e = m.table.slots[idx]
            assert e.key.lane == lane
            check_forward(m, lane, frame, out, e)
        else:
            body = out.data[pm.PUNT_HDR_LEN:] if m.punt_hdr and out.reason != pm.RSN_BYPASS else out.data
            assert body == frame, "punted frames are unchanged"
            if out.hit_idx is not None:
                assert m.table.slots[out.hit_idx].key == pm.parse(frame, lane).key
    return fwd


@pytest.mark.parametrize("seed,punt_hdr,egress_en,seeds", [
    (1, True, 0xff, (0xffffffff, 0xffffffff)),
    (2, False, 0xff, (0xffffffff, 0xffffffff)),
    (3, True, 0xbf, (0x1234abcd, 0x5555aaaa)),
])
def test_forwarding_checked_independently(seed, punt_hdr, egress_en, seeds):
    random.seed(seed)
    m = make_model(7, punt_hdr=punt_hdr, egress_en=egress_en, seeds=seeds)
    entries, nhs = make_world(300)
    program(m, entries, nhs)
    items = traffic(entries, 3000)
    fwd = run_traffic(m, entries, items)
    assert fwd > 300 and len(items) - fwd > 300
    assert sum(m.stats.values()) == len(items)


def test_bypass_and_disabled():
    random.seed(9)
    for enable, bypass in ((False, 0), (True, 0x0f)):
        m = make_model(5, enable=enable, bypass=bypass)
        entries, nhs = make_world(50)
        program(m, entries, nhs)
        for lane, frame in traffic(entries, 400):
            out = m.process(lane, frame)
            if not enable or (bypass >> lane) & 1:
                assert (out.kind, out.reason, out.data) == ("punt", pm.RSN_BYPASS, frame)


def test_every_reason_reachable():
    """The traffic mix exercises every punt reason and forwarding."""
    random.seed(10)
    m = make_model(6)
    entries, nhs = make_world(100)
    program(m, entries, nhs)
    run_traffic(m, entries, traffic(entries, 4000))
    seen = {r for (lane, r), n in m.stats.items() if n}
    expected = {pm.RSN_MISS, pm.RSN_NOT_IPV4, pm.RSN_MCAST, pm.RSN_IP_HDR, pm.RSN_FRAG, pm.RSN_TTL,
                pm.RSN_PROTO, pm.RSN_CSUM, pm.RSN_SYN, pm.RSN_FINRST, pm.RSN_VLAN, pm.RSN_NH, pm.RSN_FWD}
    assert expected <= seen, expected - seen
