# SPDX-License-Identifier: BSD-3-Clause
"""Bit layouts, hashes and punt-header parsing agree with the Python model
(and so with rtl/natgw_pkg.sv)."""

import random
import struct

import ctypes as C
import pytest

from natgw_c import (Entry, Key, NextHop, Punt, State, entry_c, key_c, lib, nh_c, pm, words, words_int)


def rand_key():
    return pm.Key(lane=random.randrange(8), vid=random.randrange(4096), tcp=random.randint(0, 1),
                  sip=random.getrandbits(32), dip=random.getrandbits(32),
                  sport=random.getrandbits(16), dport=random.getrandbits(16))


@pytest.mark.parametrize("seed", range(5))
def test_entry_pack_matches_model(seed):
    random.seed(seed)
    for _ in range(500):
        e = pm.Entry(key=rand_key(), xlate_dst=random.randint(0, 1), new_ip=random.getrandbits(32),
                     new_port=random.getrandbits(16), dec_ttl=random.randint(0, 1),
                     nh_idx=random.randrange(1024), valid=random.randint(0, 1))
        w = words(7)
        lib.natgw_entry_pack(C.byref(entry_c(e)), w)
        assert words_int(w) == e.pack()
        back = Entry()
        lib.natgw_entry_unpack(w, C.byref(back))
        assert (back.valid, back.xlate_dst, back.new_ip, back.new_port, back.dec_ttl, back.nh_idx) == \
            (e.valid, e.xlate_dst, e.new_ip, e.new_port, e.dec_ttl, e.nh_idx)
        assert (back.key.lane, back.key.vid, back.key.tcp, back.key.sip, back.key.dip, back.key.sport,
                back.key.dport) == (e.key.lane, e.key.vid, e.key.tcp, e.key.sip, e.key.dip, e.key.sport,
                                    e.key.dport)


def test_state_pack_matches_model():
    random.seed(7)
    for _ in range(500):
        s = pm.State(valid=random.randint(0, 1), fin=random.randint(0, 1), rst=random.randint(0, 1),
                     evp=random.randint(0, 1), tcp=random.randint(0, 1), ts=random.getrandbits(32),
                     pkts=random.getrandbits(48), bytes=random.getrandbits(56))
        cs = State(s.valid, s.fin, s.rst, s.evp, s.tcp, s.ts, s.pkts, s.bytes)
        w = words(5)
        lib.natgw_state_pack(C.byref(cs), w)
        assert words_int(w) == s.pack()
        back = State()
        lib.natgw_state_unpack(w, C.byref(back))
        assert (back.valid, back.fin, back.rst, back.evp, back.tcp, back.ts, back.pkts, back.bytes) == \
            (s.valid, s.fin, s.rst, s.evp, s.tcp, s.ts, s.pkts, s.bytes)


def test_nh_pack_matches_model():
    random.seed(8)
    for _ in range(500):
        n = pm.NextHop(dst_mac=random.getrandbits(48), src_mac=random.getrandbits(48),
                       lane=random.randrange(8), vlan=random.randint(0, 1), vid=random.randrange(4096),
                       valid=random.randint(0, 1))
        w = words(4)
        lib.natgw_nh_pack(C.byref(nh_c(n)), w)
        assert words_int(w) == n.pack()
        back = NextHop()
        lib.natgw_nh_unpack(w, C.byref(back))
        assert int.from_bytes(bytes(back.dst_mac), 'big') == n.dst_mac
        assert int.from_bytes(bytes(back.src_mac), 'big') == n.src_mac
        assert (back.lane, back.vlan, back.vid, back.valid) == (n.lane, n.vlan, n.vid, n.valid)


def spec_crc(key_bits, seed, poly):
    """the spec, written out: reflected CRC over the 112 key bits, bit 0 first, from seed, no final XOR"""
    crc = seed
    for i in range(112):
        if (crc ^ (key_bits >> i)) & 1:
            crc = (crc >> 1) ^ poly
        else:
            crc >>= 1
    return crc


@pytest.mark.parametrize("seed0,seed1", [(0xffffffff, 0xffffffff), (0, 0), (0x12345678, 0x9abcdef0)])
def test_hashes_match_spec(seed0, seed1):
    random.seed(seed0 ^ seed1)
    for _ in range(1000):
        k = rand_key()
        kb = k.pack()
        assert lib.natgw_key_crc(C.byref(key_c(k)), seed0, 0x82F63B78) == spec_crc(kb, seed0, 0x82F63B78)
        assert lib.natgw_key_crc(C.byref(key_c(k)), seed1, 0xEDB88320) == spec_crc(kb, seed1, 0xEDB88320)


def test_punt_parse():
    hdr = struct.pack(">HBBBBHII", 0x4E47, 1, 10, 5, 0x0d, 0x6064, 0xdeadbeef, 1234)
    buf = (C.c_uint8 * 16).from_buffer_copy(hdr)
    p = Punt()
    assert lib.natgw_punt_parse(buf, 16, C.byref(p)) == 0
    assert (p.reason, p.lane, p.flags, p.tci, p.hash, p.idx) == (10, 5, 0x0d, 0x6064, 0xdeadbeef, 1234)
    assert lib.natgw_punt_parse(buf, 15, C.byref(p)) < 0
    bad = (C.c_uint8 * 16).from_buffer_copy(b"\x00" + hdr[1:])
    assert lib.natgw_punt_parse(bad, 16, C.byref(p)) < 0
    bad_ver = (C.c_uint8 * 16).from_buffer_copy(hdr[:2] + b"\x02" + hdr[3:])
    assert lib.natgw_punt_parse(bad_ver, 16, C.byref(p)) < 0
