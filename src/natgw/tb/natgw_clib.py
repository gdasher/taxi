# SPDX-License-Identifier: BSD-3-Clause
"""ctypes binding for libnatgw (library and C software model).

The C model is the one behavioural reference of the natgw shim: the RTL
testbenches use it as their scoreboard (through natgw_model.py) and the host
software runs against it. The library is rebuilt here when its sources are
newer than the build.
"""

import ctypes as C
import glob
import os
import subprocess

SW = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sw'))
LIB = os.path.join(SW, 'build', 'libnatgw.so')


def _stale():
    if not os.path.exists(LIB):
        return True
    t = os.path.getmtime(LIB)
    srcs = glob.glob(os.path.join(SW, 'lib', '*.c')) + glob.glob(os.path.join(SW, 'model', '*.c')) + \
        glob.glob(os.path.join(SW, 'include', '*.h'))
    return any(os.path.getmtime(f) > t for f in srcs)


if _stale():
    subprocess.run(["make", "-s", "-C", SW], check=True)

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
_sig("natgw_dev_ddr_read_errors", C.c_uint32, P(Dev))
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


class Parsed(C.Structure):
    _fields_ = [("reason", C.c_uint8), ("lookup", C.c_uint8), ("l3ok", C.c_uint8), ("vlan", C.c_uint8),
                ("tci", C.c_uint16), ("ttl", C.c_uint8), ("tcp", C.c_uint8), ("ip_off", C.c_uint32),
                ("fin", C.c_uint8), ("rst", C.c_uint8), ("key", Key)]


_sig("natgw_table_candidates", C.c_uint, C.c_void_p, P(Key), P(C.c_uint32))
_sig("natgw_oc_sum16", C.c_uint16, P(C.c_uint8), C.c_size_t)
_sig("natgw_csum_update3", C.c_uint16, *([C.c_uint16] * 7))
_sig("natgw_model_parse", None, P(C.c_uint8), C.c_size_t, C.c_uint, C.c_bool, P(Parsed))
