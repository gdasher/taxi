# SPDX-License-Identifier: BSD-3-Clause
"""ctypes binding for libnatgw (library and software model), for tests."""

import ctypes as C
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, '..', 'build', 'libnatgw.so')
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', 'tb')))
import natgw_model as pm  # noqa: E402  (the Python model; scoreboard of the RTL tests)

lib = C.CDLL(LIB)

LANES = 8
PUNT_HDR_LEN = 16


class Key(C.Structure):
    _fields_ = [("lane", C.c_uint8), ("vid", C.c_uint16), ("tcp", C.c_uint8),
                ("sip", C.c_uint32), ("dip", C.c_uint32), ("sport", C.c_uint16), ("dport", C.c_uint16)]


class Entry(C.Structure):
    _fields_ = [("key", Key), ("valid", C.c_uint8), ("xlate_dst", C.c_uint8), ("new_ip", C.c_uint32),
                ("new_port", C.c_uint16), ("dec_ttl", C.c_uint8), ("nh_idx", C.c_uint16)]


class NextHop(C.Structure):
    _fields_ = [("valid", C.c_uint8), ("dst_mac", C.c_uint8 * 6), ("src_mac", C.c_uint8 * 6),
                ("lane", C.c_uint8), ("vlan", C.c_uint8), ("vid", C.c_uint16)]


class State(C.Structure):
    _fields_ = [("valid", C.c_uint8), ("fin", C.c_uint8), ("rst", C.c_uint8), ("evp", C.c_uint8),
                ("tcp", C.c_uint8), ("ts", C.c_uint32), ("pkts", C.c_uint64), ("bytes", C.c_uint64)]


class Event(C.Structure):
    _fields_ = [("type", C.c_uint8), ("idx", C.c_uint32), ("tick", C.c_uint32)]


class Punt(C.Structure):
    _fields_ = [("reason", C.c_uint8), ("lane", C.c_uint8), ("flags", C.c_uint8), ("tci", C.c_uint16),
                ("hash", C.c_uint32), ("idx", C.c_uint32)]


RD = C.CFUNCTYPE(C.c_uint32, C.c_void_p, C.c_uint32)
WR = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_uint32)


class Io(C.Structure):
    _fields_ = [("rd", RD), ("wr", WR), ("ctx", C.c_void_p)]


class Dev(C.Structure):
    _fields_ = [("io", Io), ("version", C.c_uint32), ("idx_w", C.c_uint), ("bucket_w", C.c_uint),
                ("lanes", C.c_uint), ("punt_hdr_len", C.c_uint)]


class Write(C.Structure):
    _fields_ = [("idx", C.c_uint32), ("clear", C.c_bool), ("entry", Entry)]


class NhTable(C.Structure):
    _fields_ = [("nh", NextHop * 1024), ("refs", C.c_uint32 * 1024)]


class ModelOut(C.Structure):
    _fields_ = [("kind", C.c_int), ("lane", C.c_uint8), ("reason", C.c_uint8), ("hit_idx", C.c_uint32),
                ("len", C.c_size_t), ("data", C.POINTER(C.c_uint8))]


OUT_FWD = 1
OUT_PUNT = 2


def _sig(name, res, *args):
    f = getattr(lib, name)
    f.restype = res
    f.argtypes = list(args)
    return f


P = C.POINTER
_sig("natgw_entry_pack", None, P(Entry), P(C.c_uint32))
_sig("natgw_entry_unpack", None, P(C.c_uint32), P(Entry))
_sig("natgw_state_pack", None, P(State), P(C.c_uint32))
_sig("natgw_state_unpack", None, P(C.c_uint32), P(State))
_sig("natgw_nh_pack", None, P(NextHop), P(C.c_uint32))
_sig("natgw_nh_unpack", None, P(C.c_uint32), P(NextHop))
_sig("natgw_key_crc", C.c_uint32, P(Key), C.c_uint32, C.c_uint32)
_sig("natgw_punt_parse", C.c_int, P(C.c_uint8), C.c_size_t, P(Punt))
_sig("natgw_dev_init", C.c_int, P(Dev), P(Io))
_sig("natgw_dev_flush", None, P(Dev))
_sig("natgw_dev_clear", C.c_int, P(Dev), C.c_uint)
_sig("natgw_dev_set_ctrl", None, P(Dev), C.c_bool, C.c_bool, C.c_uint8, C.c_uint8)
_sig("natgw_dev_set_seeds", None, P(Dev), C.c_uint32, C.c_uint32)
_sig("natgw_dev_set_thresholds", None, P(Dev), C.c_uint32, C.c_uint32)
_sig("natgw_dev_tick", C.c_uint32, P(Dev))
_sig("natgw_dev_write_entry", None, P(Dev), C.c_uint32, P(Entry))
_sig("natgw_dev_clear_entry", None, P(Dev), C.c_uint32)
_sig("natgw_dev_read_entry", None, P(Dev), C.c_uint32, P(Entry))
_sig("natgw_dev_read_state", None, P(Dev), C.c_uint32, P(State))
_sig("natgw_dev_write_nh", None, P(Dev), C.c_uint16, P(NextHop))
_sig("natgw_dev_read_nh", None, P(Dev), C.c_uint16, P(NextHop))
_sig("natgw_dev_pop_event", C.c_int, P(Dev), P(Event))
_sig("natgw_dev_event_drops", C.c_uint32, P(Dev))
_sig("natgw_dev_read_stat", C.c_uint64, P(Dev), C.c_uint, C.c_uint)
_sig("natgw_dev_apply", None, P(Dev), P(Write), C.c_uint)


class DdrStatus(C.Structure):
    _fields_ = [("present", C.c_bool), ("calibrated", C.c_bool), ("enabled", C.c_bool),
                ("clearing", C.c_bool), ("active", C.c_bool), ("bucket_w", C.c_uint), ("max_out", C.c_uint)]


_sig("natgw_dev_ddr_status", None, P(Dev), P(DdrStatus))
_sig("natgw_dev_ddr_clear", C.c_int, P(Dev), C.c_uint)
_sig("natgw_dev_ddr_enable", None, P(Dev), C.c_bool)
_sig("natgw_dev_write_ddr_entry", None, P(Dev), C.c_uint32, P(Entry))
_sig("natgw_dev_clear_ddr_entry", None, P(Dev), C.c_uint32)
_sig("natgw_dev_read_ddr_entry", None, P(Dev), C.c_uint32, P(Entry))
_sig("natgw_dev_read_activity", C.c_uint64, P(Dev), C.c_uint32)
_sig("natgw_dev_ddr_stats", None, P(Dev), P(C.c_uint32), P(C.c_uint32), P(C.c_uint32))
_sig("natgw_dev_apply_ddr", None, P(Dev), P(Write), C.c_uint)
_sig("natgw_table_create", C.c_void_p, C.c_uint, C.c_uint32, C.c_uint32, C.c_uint)
_sig("natgw_table_create_ddr", C.c_void_p, C.c_uint, C.c_uint32, C.c_uint32, C.c_uint)
_sig("natgw_table_destroy", None, C.c_void_p)
_sig("natgw_table_size", C.c_uint, C.c_void_p)
_sig("natgw_table_count", C.c_uint, C.c_void_p)
_sig("natgw_table_insert", C.c_int, C.c_void_p, P(Entry), P(Write), C.c_uint, P(C.c_uint32))
_sig("natgw_table_delete", C.c_int, C.c_void_p, P(Key), P(Write), C.c_uint)
_sig("natgw_table_find", C.c_int, C.c_void_p, P(Key), P(C.c_uint32))
_sig("natgw_table_lookup", C.c_int, C.c_void_p, P(Key), P(C.c_uint32))
_sig("natgw_table_slot", P(Entry), C.c_void_p, C.c_uint32)
_sig("natgw_table_clear", None, C.c_void_p)
_sig("natgw_nh_table_init", None, P(NhTable))
_sig("natgw_nh_get", C.c_int, P(NhTable), P(NextHop), P(C.c_bool))
_sig("natgw_nh_put", C.c_int, P(NhTable), C.c_uint16)
_sig("natgw_model_create", C.c_void_p, C.c_uint)
_sig("natgw_model_destroy", None, C.c_void_p)
_sig("natgw_model_io", Io, C.c_void_p)
_sig("natgw_model_rd", C.c_uint32, C.c_void_p, C.c_uint32)
_sig("natgw_model_wr", None, C.c_void_p, C.c_uint32, C.c_uint32)
_sig("natgw_model_rx", C.c_int, C.c_void_p, C.c_uint, P(C.c_uint8), C.c_size_t, P(ModelOut))
_sig("natgw_model_advance", None, C.c_void_p, C.c_uint32)
_sig("natgw_model_set_ddr", C.c_int, C.c_void_p, C.c_uint, C.c_bool)


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
