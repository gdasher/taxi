# SPDX-License-Identifier: BSD-3-Clause
"""ctypes binding for libnatgw (library and software model), for tests: the
shared binding (tb/natgw_clib.py) plus conversions and small wrappers."""

import ctypes as C
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', 'tb')))
from natgw_clib import *  # noqa: E402,F401,F403
from natgw_clib import P, lib  # noqa: E402
import natgw_model as pm  # noqa: E402  (data types; behaviour is the C model)

# ---------------------------------------------------------------- conversions

def key_c(k):
    return Key(k.lane, k.vid, k.tcp, k.sip, k.dip, k.sport, k.dport)


def key_py(k):
    return pm.Key(k.lane, k.vid, k.tcp, k.sip, k.dip, k.sport, k.dport)


def entry_c(e):
    return Entry(key_c(e.key), e.valid, e.xlate_dst, e.new_ip, e.new_port, e.dec_ttl, e.nh_idx)


def entry_py(e):
    return pm.Entry(key=key_py(e.key), xlate_dst=e.xlate_dst, new_ip=e.new_ip, new_port=e.new_port,
                    dec_ttl=e.dec_ttl, nh_idx=e.nh_idx, valid=e.valid)


def mac_bytes(v):
    return (C.c_uint8 * 6)(*v.to_bytes(6, 'big'))


def nh_c(n):
    return NextHop(n.valid, mac_bytes(n.dst_mac), mac_bytes(n.src_mac), n.lane, n.vlan, n.vid)


def words(n):
    return (C.c_uint32 * n)()


def words_int(w):
    return sum(int(x) << (32 * i) for i, x in enumerate(w))


# ---------------------------------------------------------------- wrappers

class Model:
    """The C software model, driven through libnatgw's device API."""

    def __init__(self, bucket_w, ddr_bucket_w=None, ddr_calib=True):
        self.h = lib.natgw_model_create(bucket_w)
        assert self.h
        if ddr_bucket_w is not None:
            assert lib.natgw_model_set_ddr(self.h, ddr_bucket_w, ddr_calib) == 0
        self.io = lib.natgw_model_io(self.h)
        self.dev = Dev()
        rc = lib.natgw_dev_init(C.byref(self.dev), C.byref(self.io))
        assert rc == 0, rc
        self.buf = (C.c_uint8 * 16384)()

    def close(self):
        if self.h:
            lib.natgw_model_destroy(self.h)
            self.h = None

    def rx(self, lane, frame):
        f = (C.c_uint8 * len(frame)).from_buffer_copy(frame)
        out = ModelOut(0, 0, 0, 0, 0, C.cast(self.buf, P(C.c_uint8)))
        rc = lib.natgw_model_rx(self.h, lane, f, len(frame), C.byref(out))
        assert rc == 0, rc
        return out.kind, out.lane, out.reason, out.hit_idx, bytes(self.buf[:out.len])

    def advance(self, ticks):
        lib.natgw_model_advance(self.h, ticks)


class Table:
    def __init__(self, bucket_w, seed0=0xffffffff, seed1=0xffffffff, max_depth=6, ddr=False):
        create = lib.natgw_table_create_ddr if ddr else lib.natgw_table_create
        self.h = create(bucket_w, seed0, seed1, max_depth)
        assert self.h
        self.max_ops = max_depth + 2
        self.ops = (Write * self.max_ops)()

    def close(self):
        if self.h:
            lib.natgw_table_destroy(self.h)
            self.h = None

    def insert(self, e, max_ops=None):
        idx = C.c_uint32()
        n = lib.natgw_table_insert(self.h, C.byref(e), self.ops, max_ops or self.max_ops, C.byref(idx))
        # copies: the ctypes buffer is reused by the next call
        return n, idx.value, [Write.from_buffer_copy(self.ops[i]) for i in range(max(n, 0))]

    def delete(self, k):
        n = lib.natgw_table_delete(self.h, C.byref(k), self.ops, self.max_ops)
        return n, [Write.from_buffer_copy(self.ops[i]) for i in range(max(n, 0))]

    def lookup(self, k):
        idx = C.c_uint32()
        rc = lib.natgw_table_lookup(self.h, C.byref(k), C.byref(idx))
        return idx.value if rc == 0 else None

    @property
    def size(self):
        return lib.natgw_table_size(self.h)

    @property
    def count(self):
        return lib.natgw_table_count(self.h)
