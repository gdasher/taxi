# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim reference model: the Python face of the C model

Data types and bit layouts (natgw_pkg.sv) are defined here; every behaviour
(hashes, checksums, classification, cuckoo placement, the whole shim) comes
from the C software model and libnatgw (sw/, through natgw_clib), the same
code the host software runs against. So the RTL testbenches, the host tests
and the DPDK/VPP stacks all check against one reference.

"""

import ctypes as C
from dataclasses import dataclass

import natgw_clib as clib
import natgw_cov as cov

LANES = 8

RSN_MISS = 0
RSN_BYPASS = 1
RSN_NOT_IPV4 = 2
RSN_MCAST = 3
RSN_IP_HDR = 4
RSN_FRAG = 5
RSN_TTL = 6
RSN_PROTO = 7
RSN_CSUM = 8
RSN_SYN = 9
RSN_FINRST = 10
RSN_VLAN = 11
RSN_NH = 12
RSN_FWD = 15

RSN_NAMES = {
    RSN_MISS: "miss", RSN_BYPASS: "bypass", RSN_NOT_IPV4: "not_ipv4", RSN_MCAST: "mcast",
    RSN_IP_HDR: "ip_hdr", RSN_FRAG: "frag", RSN_TTL: "ttl", RSN_PROTO: "proto",
    RSN_CSUM: "csum", RSN_SYN: "syn", RSN_FINRST: "finrst", RSN_VLAN: "vlan",
    RSN_NH: "nh", RSN_FWD: "fwd",
}

EVT_IDLE = 1
EVT_FIN = 2
EVT_RST = 3
EVT_OVF = 15

PUNT_MAGIC = 0x4E47
PUNT_VER = 1
PUNT_HDR_LEN = 16

POLY_CRC32C = 0x82F63B78
POLY_CRC32 = 0xEDB88320

KEY_W = 112
ENTRY_W = 216
NH_W = 128
STATE_W = 144


# ---------------------------------------------------------------------------
# bit-field packing (LSB-first field lists, matching the packed structs)

def _pack(fields, values):
    v = 0
    pos = 0
    for name, w in fields:
        x = values.get(name, 0)
        if isinstance(x, int):
            x &= (1 << w) - 1
        else:
            x = int(x) & ((1 << w) - 1)
        v |= x << pos
        pos += w
    return v


def _unpack(fields, v):
    out = {}
    pos = 0
    for name, w in fields:
        out[name] = (v >> pos) & ((1 << w) - 1)
        pos += w
    return out


KEY_FIELDS = [("lane", 3), ("vid", 12), ("tcp", 1), ("sip", 32), ("dip", 32), ("sport", 16), ("dport", 16)]
ENTRY_FIELDS = [("valid", 1), ("key", 112), ("xlate_dst", 1), ("new_ip", 32), ("new_port", 16),
                ("dec_ttl", 1), ("nh_idx", 10), ("rsvd", 43)]
NH_FIELDS = [("valid", 1), ("dst_mac", 48), ("src_mac", 48), ("lane", 3), ("vlan", 1), ("vid", 12), ("rsvd", 15)]
STATE_FIELDS = [("valid", 1), ("fin", 1), ("rst", 1), ("evp", 1), ("tcp", 1), ("rsvd", 3),
                ("ts", 32), ("pkts", 48), ("bytes", 56)]


@dataclass(frozen=True)
class Key:
    lane: int
    vid: int
    tcp: int
    sip: int
    dip: int
    sport: int
    dport: int

    def pack(self):
        return _pack(KEY_FIELDS, self.__dict__)

    @classmethod
    def unpack(cls, v):
        return cls(**_unpack(KEY_FIELDS, v))


@dataclass
class Entry:
    key: Key
    xlate_dst: int = 0
    new_ip: int = 0
    new_port: int = 0
    dec_ttl: int = 1
    nh_idx: int = 0
    valid: int = 1

    def pack(self):
        d = dict(self.__dict__)
        d["key"] = self.key.pack()
        return _pack(ENTRY_FIELDS, d)

    @classmethod
    def unpack(cls, v):
        d = _unpack(ENTRY_FIELDS, v)
        d.pop("rsvd")
        d["key"] = Key.unpack(d["key"])
        return cls(**d)


@dataclass
class NextHop:
    dst_mac: int
    src_mac: int
    lane: int
    vlan: int = 0
    vid: int = 0
    valid: int = 1

    def pack(self):
        return _pack(NH_FIELDS, self.__dict__)

    @classmethod
    def unpack(cls, v):
        d = _unpack(NH_FIELDS, v)
        d.pop("rsvd")
        return cls(**d)


@dataclass
class State:
    valid: int = 0
    fin: int = 0
    rst: int = 0
    evp: int = 0
    tcp: int = 0
    ts: int = 0
    pkts: int = 0
    bytes: int = 0

    def pack(self):
        return _pack(STATE_FIELDS, self.__dict__)

    @classmethod
    def unpack(cls, v):
        d = _unpack(STATE_FIELDS, v)
        d.pop("rsvd")
        return cls(**d)


def to_words(v, n):
    return [(v >> (32 * k)) & 0xffffffff for k in range(n)]


def from_words(words):
    v = 0
    for k, w in enumerate(words):
        v |= (w & 0xffffffff) << (32 * k)
    return v


# ---------------------------------------------------------------------------
# conversions to and from the C structures

def _key_c(k):
    return clib.Key(k.lane, k.vid, k.tcp, k.sip, k.dip, k.sport, k.dport)


def _key_py(k):
    return Key(k.lane, k.vid, k.tcp, k.sip, k.dip, k.sport, k.dport)


def _entry_c(e):
    return clib.Entry(_key_c(e.key), e.valid, e.xlate_dst, e.new_ip, e.new_port, e.dec_ttl, e.nh_idx)


def _entry_py(e):
    return Entry(key=_key_py(e.key), xlate_dst=e.xlate_dst, new_ip=e.new_ip, new_port=e.new_port,
                 dec_ttl=e.dec_ttl, nh_idx=e.nh_idx, valid=e.valid)


def _nh_c(n):
    mac = lambda v: (C.c_uint8 * 6)(*v.to_bytes(6, 'big'))  # noqa: E731
    return clib.NextHop(n.valid, mac(n.dst_mac), mac(n.src_mac), n.lane, n.vlan, n.vid)


def _buf(data):
    data = bytes(data)
    return (C.c_uint8 * max(len(data), 1)).from_buffer_copy(data or b"\0"), len(data)


# ---------------------------------------------------------------------------
# hashes and checksums (C)

def key_crc(key_bits, seed, poly):
    """reflected CRC over the 112-bit key, bit 0 first, from seed, no final XOR"""
    return clib.lib.natgw_key_crc(C.byref(_key_c(Key.unpack(key_bits))), seed & 0xffffffff, poly)


def oc_sum16(data):
    """One's complement sum of 16-bit big-endian words (data length even)."""
    b, n = _buf(data)
    return clib.lib.natgw_oc_sum16(b, n)


def csum_update3(hc, m0, n0, m1, n1, m2, n2):
    """RFC 1624 eqn. 3, identical arithmetic to natgw_pkg::csum_update3."""
    return clib.lib.natgw_csum_update3(hc, m0, n0, m1, n1, m2, n2)


# ---------------------------------------------------------------------------
# frame parsing and classification (C)

@dataclass
class Parsed:
    reason: int
    lookup: bool
    l3ok: bool
    vlan: int
    tci: int
    key: Key
    ttl: int = 0
    tcp: int = 0
    ip_off: int = 14
    fin: int = 0
    rst: int = 0


def _b(frame, i):
    return frame[i] if i < len(frame) else 0


def _w16(frame, i):
    return (_b(frame, i) << 8) | _b(frame, i+1)


def _w32(frame, i):
    return (_w16(frame, i) << 16) | _w16(frame, i+2)


def parse(frame, lane, bypass=False):
    """Classify a frame exactly as natgw_parser.sv does (only the first 64 bytes are visible)."""
    b, n = _buf(frame)
    p = clib.Parsed()
    clib.lib.natgw_model_parse(b, n, lane, bool(bypass), C.byref(p))
    return Parsed(reason=p.reason, lookup=bool(p.lookup), l3ok=bool(p.l3ok), vlan=p.vlan, tci=p.tci,
                  key=_key_py(p.key), ttl=p.ttl, tcp=p.tcp, ip_off=p.ip_off, fin=p.fin, rst=p.rst)


# ---------------------------------------------------------------------------
# cuckoo tables (libnatgw's host table)

class TableFull(Exception):
    pass


class CuckooTable:
    """Host copy of the two-table, 4-slot cuckoo table (libnatgw). insert()
    returns the ordered (idx, Entry) writes, relocations first; delete()
    returns [(idx, None)]. slots (idx -> Entry) and where (Key -> idx) mirror
    the table, updated from those writes."""

    SLOTS = 4
    TABLES = 2
    _create = "natgw_table_create"

    def __init__(self, bucket_w, seed0=0xffffffff, seed1=0xffffffff, max_depth=6):
        self.bucket_w = bucket_w
        self.buckets = 1 << bucket_w
        self.seed0 = seed0
        self.seed1 = seed1
        self.max_depth = max_depth
        self.h = getattr(clib.lib, self._create)(bucket_w, seed0 & 0xffffffff, seed1 & 0xffffffff, max_depth)
        if not self.h:
            raise ValueError(f"table geometry bucket_w={bucket_w} refused")
        self.max_ops = max_depth + 2
        self._ops = (clib.Write * self.max_ops)()
        self.slots = {}     # idx -> Entry
        self.where = {}     # Key -> idx

    def __del__(self):
        if getattr(self, "h", None):
            clib.lib.natgw_table_destroy(self.h)
            self.h = None

    @property
    def idx_w(self):
        return self.bucket_w + 3

    @property
    def size(self):
        return self.TABLES * self.buckets * self.SLOTS

    def hashes(self, key):
        kb = key.pack()
        return key_crc(kb, self.seed0, POLY_CRC32C), key_crc(kb, self.seed1, POLY_CRC32)

    # index layout ({table, bucket, slot}): an address format, not behaviour
    def idx(self, t, bucket, slot):
        return (t << (self.bucket_w + 2)) | (bucket << 2) | slot

    def split_idx(self, idx):
        return idx >> (self.bucket_w + 2), (idx >> 2) & (self.buckets - 1), idx & 3

    def line_of(self, idx):
        """(table, bucket) of an index"""
        t, b, _ = self.split_idx(idx)
        return t, b

    def bucket_of(self, key, t):
        return self.split_idx(list(self.candidates(key))[t * self.SLOTS])[1]

    def candidates(self, key):
        c = (C.c_uint32 * 8)()
        n = clib.lib.natgw_table_candidates(self.h, C.byref(_key_c(key)), c)
        return list(c[:n])

    def lookup(self, key):
        """Hardware lookup order: first valid matching candidate."""
        idx = C.c_uint32()
        if clib.lib.natgw_table_lookup(self.h, C.byref(_key_c(key)), C.byref(idx)) != 0:
            return None, None
        return idx.value, self.slots[idx.value]

    def _writes(self, n):
        out = []
        for i in range(n):
            op = self._ops[i]
            if op.clear:
                out.append((op.idx, None))
                old = self.slots.pop(op.idx, None)
                if old is not None and self.where.get(old.key) == op.idx:
                    del self.where[old.key]
            else:
                e = _entry_py(op.entry)
                old = self.slots.get(op.idx)
                if old is not None and self.where.get(old.key) == op.idx:
                    del self.where[old.key]
                self.slots[op.idx] = e
                self.where[e.key] = op.idx
                out.append((op.idx, e))
        return out

    def insert(self, entry):
        idx = C.c_uint32()
        n = clib.lib.natgw_table_insert(self.h, C.byref(_entry_c(entry)), self._ops, self.max_ops, C.byref(idx))
        tier = "ddr" if isinstance(self, DdrTable) else "uram"
        if n == -28:            # ENOSPC
            cov.hit("insert", tier, "full")
            raise TableFull()
        if n < 0:
            raise ValueError(f"insert refused: {n}")
        cov.hit("insert", tier, str(n) if n < 5 else "5+")
        w = self._writes(n)
        # the last write is the new entry itself, as given (its dataclass identity kept)
        self.slots[w[-1][0]] = entry
        return w[:-1] + [(w[-1][0], entry)]

    def delete(self, key):
        n = clib.lib.natgw_table_delete(self.h, C.byref(_key_c(key)), self._ops, self.max_ops)
        if n < 0:
            raise KeyError(key)
        return self._writes(n)

    def load(self):
        return len(self.slots) / self.size


class DdrTable(CuckooTable):
    """Host copy of the DDR tier: two tables of 64-byte lines holding two
    entries each; buckets come from the top bits of the hashes (the UltraRAM
    tier uses the low bits). Index = {table, bucket, slot}."""

    SLOTS = 2
    _create = "natgw_table_create_ddr"

    @property
    def idx_w(self):
        return self.bucket_w + 2

    def idx(self, t, bucket, slot):
        return (t << (self.bucket_w + 1)) | (bucket << 1) | slot

    def split_idx(self, idx):
        return idx >> (self.bucket_w + 1), (idx >> 1) & (self.buckets - 1), idx & 1

    def line(self, t, bucket):
        """line number {table, bucket}: the line's address is base + 64 * line"""
        return (t << self.bucket_w) | bucket


DDR_IDX_FLAG = 0x80000000


# ---------------------------------------------------------------------------
# full shim model (the C model)

@dataclass
class Output:
    kind: str                 # "fwd" or "punt"
    lane: int                 # egress lane (fwd) or ingress lane (punt)
    data: bytes
    reason: int = RSN_FWD
    hit_idx: int = None
    fin: int = 0
    rst: int = 0


class _ModelTable:
    """a table whose writes also go to the C model's registers"""

    def __init__(self, table, apply):
        self._t = table
        self._apply = apply

    def __getattr__(self, name):
        return getattr(self._t, name)

    def _push(self, writes):
        ops = (clib.Write * max(len(writes), 1))()
        for k, (i, e) in enumerate(writes):
            ops[k].idx = i
            ops[k].clear = e is None
            if e is not None:
                ops[k].entry = _entry_c(e)
        self._apply(ops, len(writes))
        return writes

    def insert(self, entry):
        return self._push(self._t.insert(entry))

    def delete(self, key):
        return self._push(self._t.delete(key))


class _ModelNh(dict):
    """next hops; every assignment is written to the C model"""

    def __init__(self, model):
        super().__init__()
        self._m = model

    def __setitem__(self, idx, nh):
        super().__setitem__(idx, nh)
        clib.lib.natgw_dev_write_nh(C.byref(self._m.dev), idx, C.byref(_nh_c(nh)))

    def update(self, *args, **kw):
        for idx, nh in dict(*args, **kw).items():
            self[idx] = nh

    def setdefault(self, idx, nh=None):
        if idx not in self:
            self[idx] = nh
        return self[idx]

    def __delitem__(self, idx):
        super().__delitem__(idx)
        clib.lib.natgw_dev_write_nh(C.byref(self._m.dev), idx, C.byref(clib.NextHop()))

    def pop(self, idx, *default):
        if idx in self:
            v = self[idx]
            del self[idx]
            return v
        return dict.pop(self, idx, *default)


class ShimModel:
    """The whole shim, as the C software model (natgw_model.c) through its
    register interface: table and next-hop writes go to the model, process()
    runs a frame through it, statistics and activity are read back."""

    def __init__(self, bucket_w=10, seed0=0xffffffff, seed1=0xffffffff, enable=False, bypass_mask=0xff,
                 punt_hdr=False, egress_en=0xff, ddr_bucket_w=None):
        self.bucket_w = bucket_w
        self.seed0 = seed0
        self.seed1 = seed1
        self.h = clib.lib.natgw_model_create(bucket_w)
        if not self.h:
            raise ValueError(f"model geometry bucket_w={bucket_w} refused")
        self.ddr_bucket_w = ddr_bucket_w
        if ddr_bucket_w is not None:
            assert clib.lib.natgw_model_set_ddr(self.h, ddr_bucket_w, True) == 0
        self.io = clib.lib.natgw_model_io(self.h)
        self.dev = clib.Dev()
        assert clib.lib.natgw_dev_init(C.byref(self.dev), C.byref(self.io)) == 0
        clib.lib.natgw_dev_set_seeds(C.byref(self.dev), seed0 & 0xffffffff, seed1 & 0xffffffff)
        self._ctrl = dict(enable=enable, punt_hdr=punt_hdr, bypass_mask=bypass_mask, egress_en=egress_en)
        self._write_ctrl()
        self.table = _ModelTable(CuckooTable(bucket_w, seed0, seed1),
                                 lambda ops, n: clib.lib.natgw_dev_apply(C.byref(self.dev), ops, n))
        self.ddr = None
        self._ddr_active = False
        if ddr_bucket_w is not None:
            # DDR starts as junk, like the real memory: clear it first
            assert clib.lib.natgw_dev_ddr_clear(C.byref(self.dev), 10) == 0
            self.ddr = _ModelTable(DdrTable(ddr_bucket_w, seed0, seed1),
                                   lambda ops, n: clib.lib.natgw_dev_apply_ddr(C.byref(self.dev), ops, n))
        self.nh = _ModelNh(self)
        self._buf = (C.c_uint8 * 16384)()

    def __del__(self):
        if getattr(self, "h", None):
            clib.lib.natgw_model_destroy(self.h)
            self.h = None

    def _write_ctrl(self):
        c = self._ctrl
        clib.lib.natgw_dev_set_ctrl(C.byref(self.dev), bool(c["enable"]), bool(c["punt_hdr"]),
                                    c["bypass_mask"] & 0xff, c["egress_en"] & 0xff)

    def _ctrl_prop(name):  # noqa: N805
        def get(self):
            return self._ctrl[name]

        def set_(self, v):
            self._ctrl[name] = v
            self._write_ctrl()
        return property(get, set_)

    enable = _ctrl_prop("enable")
    punt_hdr = _ctrl_prop("punt_hdr")
    bypass_mask = _ctrl_prop("bypass_mask")
    egress_en = _ctrl_prop("egress_en")

    @property
    def ddr_active(self):
        return self._ddr_active

    @ddr_active.setter
    def ddr_active(self, v):
        self._ddr_active = bool(v)
        clib.lib.natgw_dev_ddr_enable(C.byref(self.dev), self._ddr_active)

    @property
    def stats(self):
        """{(lane, reason): count} for every non-zero counter"""
        out = {}
        for lane in range(LANES):
            for r in range(18):
                v = clib.lib.natgw_dev_read_stat(C.byref(self.dev), lane, r)
                if v:
                    out[(lane, r)] = v
        return out

    def read_activity(self, word):
        """host read-and-clear of 64 activity bits"""
        return clib.lib.natgw_dev_read_activity(C.byref(self.dev), word)

    def process(self, lane, frame):
        frame = bytes(frame)
        b, n = _buf(frame)
        out = clib.ModelOut(0, 0, 0, 0, 0, C.cast(self._buf, C.POINTER(C.c_uint8)))
        assert clib.lib.natgw_model_rx(self.h, lane, b, n, C.byref(out)) == 0
        data = bytes(self._buf[:out.len])
        hit = None if out.hit_idx == 0xffffffff else out.hit_idx
        p = parse(frame, lane, (not self.enable) or bool((self.bypass_mask >> lane) & 1))
        self._sample(lane, p, out, hit)
        if out.kind == clib.OUT_FWD:
            return Output("fwd", out.lane, data, RSN_FWD, hit)
        return Output("punt", out.lane, data, out.reason, hit, p.fin, p.rst)

    _RSN_NAME = {RSN_MISS: "miss", RSN_BYPASS: "bypass", RSN_NOT_IPV4: "not_ipv4", RSN_MCAST: "mcast",
                 RSN_IP_HDR: "ip_hdr", RSN_FRAG: "frag", RSN_TTL: "ttl", RSN_PROTO: "proto", RSN_CSUM: "csum",
                 RSN_SYN: "syn", RSN_FINRST: "finrst", RSN_VLAN: "vlan", RSN_NH: "nh", RSN_FWD: "fwd"}

    def _sample(self, lane, p, out, hit):
        if not cov.ENABLED:
            return
        fwd = out.kind == clib.OUT_FWD
        r = self._RSN_NAME.get(RSN_FWD if fwd else out.reason, str(out.reason))
        cov.hit("outcome x vlan", r, "vlan" if p.vlan else "untagged")
        cov.hit("outcome x lane", r, lane)
        if not fwd:
            cov.hit("punt header", "on" if self.punt_hdr and out.reason != RSN_BYPASS else "off")
        if hit is None:
            return
        if hit & DDR_IDX_FLAG:
            t, _, sl = self.ddr.split_idx(hit & ~DDR_IDX_FLAG)
            tier, e = "ddr", self.ddr.slots.get(hit & ~DDR_IDX_FLAG)
        else:
            t, _, sl = self.table.split_idx(hit)
            tier, e = "uram", self.table.slots.get(hit)
        cov.hit("hit slot", tier, t, sl)
        if p.fin or p.rst:
            cov.hit("fin/rst hit", tier)
        if fwd and e is not None:
            cov.hit("forward kind", "dnat" if e.xlate_dst else "snat", "tcp" if p.tcp else "udp",
                    "vlan" if p.vlan else "untagged", "dec_ttl" if e.dec_ttl else "keep_ttl")
