# SPDX-License-Identifier: BSD-3-Clause
"""Host cuckoo table: placement, relocation safety, deletes, failure modes,
and write-for-write agreement with the Python model's CuckooTable."""

import errno
import random

import ctypes as C
import pytest

from natgw_c import Entry, Key, NhTable, NextHop, Table, entry_c, key_c, key_py, lib, mac_bytes, pm


def rand_entry(lane=None):
    k = pm.Key(lane=random.randrange(8) if lane is None else lane, vid=0, tcp=random.randint(0, 1),
               sip=random.getrandbits(32), dip=random.getrandbits(32),
               sport=random.getrandbits(16), dport=random.getrandbits(16))
    return pm.Entry(key=k, xlate_dst=random.randint(0, 1), new_ip=random.getrandbits(32),
                    new_port=random.getrandbits(16), dec_ttl=1, nh_idx=random.randrange(1, 1024))


class HwSim:
    """What the hardware holds: one entry per slot, searched in hardware order."""

    def __init__(self, bucket_w, seed0=0xffffffff, seed1=0xffffffff):
        self.t = pm.CuckooTable(bucket_w, seed0, seed1)   # used only for candidate order
        self.slots = {}

    def apply(self, op):
        if op.clear:
            self.slots.pop(op.idx, None)
        else:
            self.slots[op.idx] = op.entry

    def lookup(self, key):
        for i in self.t.candidates(key):
            e = self.slots.get(i)
            if e is not None and e.valid and key_py(e.key) == key:
                return i, e
        return None, None


@pytest.mark.parametrize("bucket_w,seed", [(4, 1), (6, 2), (8, 3)])
def test_fill_relocation_never_loses_a_key(bucket_w, seed):
    """Fill until the first refused insert. After every individual hardware
    write, every present key must still be found with its own action."""
    random.seed(seed)
    t = Table(bucket_w)
    hw = HwSim(bucket_w)
    present = {}
    relocations = 0
    while True:
        e = rand_entry()
        n, idx, ops = t.insert(entry_c(e))
        if n == -errno.ENOSPC:
            break
        assert n >= 1
        relocations += n - 1
        for op in ops:
            hw.apply(op)
            for k, (act, _) in present.items():
                i, he = hw.lookup(k)
                assert he is not None, f"key lost during relocation after write to {op.idx}"
                assert (he.new_ip, he.new_port, he.xlate_dst) == act
        present[e.key] = ((e.new_ip, e.new_port, e.xlate_dst), idx)
        assert ops[-1].idx == idx
        assert t.lookup(key_c(e.key)) == idx
    load = t.count / t.size
    assert t.count == len(present)
    assert load > 0.85, load               # BFS relocation reaches high load
    assert relocations > 0
    # every key where the table says it is, and hardware agrees
    for k, (_, idx) in present.items():
        i = t.lookup(key_c(k))
        assert i is not None
        assert hw.lookup(k)[0] == i
    t.close()


def test_insert_failure_leaves_table_unchanged():
    random.seed(4)
    t = Table(3, max_depth=1)
    keys = []
    while True:
        e = rand_entry()
        n, idx, ops = t.insert(entry_c(e))
        if n < 0:
            assert n == -errno.ENOSPC
            break
        keys.append((e.key, idx))
    refused = e
    for i, (k, _) in enumerate(keys):
        keys[i] = (k, t.lookup(key_c(k)))       # relocations moved some
    before = [lib.natgw_table_slot(t.h, i) for i in range(t.size)]
    before = [(bool(p), p.contents.key.sip if p else None) for p in before]
    count = t.count
    n, _, _ = t.insert(entry_c(refused))
    assert n == -errno.ENOSPC
    after = [lib.natgw_table_slot(t.h, i) for i in range(t.size)]
    after = [(bool(p), p.contents.key.sip if p else None) for p in after]
    assert t.count == count and before == after
    for k, idx in keys:
        assert t.lookup(key_c(k)) == idx
    t.close()


def test_e2big_when_ops_buffer_too_small():
    random.seed(5)
    t = Table(3)
    while True:
        e = rand_entry()
        n, _, _ = t.insert(entry_c(e), max_ops=1)
        if n < 0:
            break
    assert n == -errno.E2BIG
    n2, _, _ = t.insert(entry_c(e))    # same entry, enough room: succeeds via relocation
    assert n2 >= 2
    t.close()


def test_update_in_place_and_delete():
    random.seed(6)
    t = Table(6)
    entries = [rand_entry() for _ in range(200)]
    where = {}
    for e in entries:
        n, idx, _ = t.insert(entry_c(e))
        assert n >= 1
        where[e.key] = idx
    for k in list(where):
        where[k] = t.lookup(key_c(k))        # relocations may have moved earlier entries
    e = entries[17]
    e2 = pm.Entry(key=e.key, xlate_dst=1 - e.xlate_dst, new_ip=e.new_ip ^ 1, new_port=e.new_port, dec_ttl=0,
                  nh_idx=e.nh_idx)
    n, idx, ops = t.insert(entry_c(e2))
    assert n == 1 and idx == where[e.key] and ops[0].entry.new_ip == e2.new_ip and ops[0].entry.dec_ttl == 0
    assert t.count == 200
    for e in entries[:50]:
        n, ops = t.delete(key_c(e.key))
        assert n == 1 and ops[0].clear and ops[0].idx == where[e.key]
        assert t.lookup(key_c(e.key)) is None
    n, _ = t.delete(key_c(entries[0].key))
    assert n == -errno.ENOENT
    assert t.count == 150
    for e in entries[50:]:
        assert t.lookup(key_c(e.key)) == where[e.key]
    t.close()


def test_rejects_invalid_entries():
    t = Table(4)
    e = entry_c(rand_entry())
    e.key.lane = 8
    assert t.insert(e)[0] == -errno.EINVAL
    e = entry_c(rand_entry())
    e.nh_idx = 1024
    assert t.insert(e)[0] == -errno.EINVAL
    t.close()


@pytest.mark.parametrize("seed0,seed1", [(0xffffffff, 0xffffffff), (0x1234, 0x8765)])
def test_placement_matches_python_model(seed0, seed1):
    """Same insert/delete sequence: identical writes from C and Python."""
    random.seed(seed0 ^ 99)
    t = Table(5, seed0, seed1)
    py = pm.CuckooTable(5, seed0, seed1)
    live = []
    for step in range(400):
        if live and random.random() < 0.2:
            k = live.pop(random.randrange(len(live)))
            n, ops = t.delete(key_c(k))
            pw = py.delete(k)
            assert n == len(pw) and [o.idx for o in ops] == [i for i, _ in pw]
            continue
        e = rand_entry()
        n, idx, ops = t.insert(entry_c(e))
        try:
            pw = py.insert(e)
        except pm.TableFull:
            assert n == -errno.ENOSPC
            continue
        assert n == len(pw), step
        assert [o.idx for o in ops] == [i for i, _ in pw], step
        assert [key_py(o.entry.key) for o in ops] == [x.key for _, x in pw], step
        live.append(e.key)
    t.close()


def test_next_hop_sharing():
    nt = NhTable()
    lib.natgw_nh_table_init(C.byref(nt))
    a = NextHop(1, mac_bytes(0x0200000000aa), mac_bytes(0x0200000000bb), 2, 0, 0)
    b = NextHop(1, mac_bytes(0x0200000000aa), mac_bytes(0x0200000000bb), 3, 0, 0)
    new = C.c_bool()
    ia = lib.natgw_nh_get(C.byref(nt), C.byref(a), C.byref(new))
    assert ia == 1 and new.value
    assert lib.natgw_nh_get(C.byref(nt), C.byref(a), C.byref(new)) == ia and not new.value
    ib = lib.natgw_nh_get(C.byref(nt), C.byref(b), C.byref(new))
    assert ib == 2 and new.value
    assert lib.natgw_nh_put(C.byref(nt), ia) == 1
    assert lib.natgw_nh_put(C.byref(nt), ia) == 0
    assert lib.natgw_nh_put(C.byref(nt), ia) < 0
    assert lib.natgw_nh_get(C.byref(nt), C.byref(a), C.byref(new)) == 1 and new.value   # slot reused
    for i in range(3, 1024):
        n = NextHop(1, mac_bytes(i), mac_bytes(0), 0, 0, 0)
        assert lib.natgw_nh_get(C.byref(nt), C.byref(n), C.byref(new)) == i
    n = NextHop(1, mac_bytes(5000), mac_bytes(0), 0, 0, 0)
    assert lib.natgw_nh_get(C.byref(nt), C.byref(n), C.byref(new)) < 0
