# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim reference model

Mirrors natgw_pkg.sv bit for bit: field layouts, hashes, classification
precedence, cuckoo table placement, rewrite and incremental checksums.
Also provides a host-side cuckoo table manager (insert with relocation,
delete) of the kind the driver (S2) will need.

"""

import struct
from collections import deque
from dataclasses import dataclass, field

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
# hashes and checksums

def key_crc(key_bits, seed, poly):
    crc = seed & 0xffffffff
    for i in range(KEY_W):
        b = (key_bits >> i) & 1
        if (crc ^ b) & 1:
            crc = (crc >> 1) ^ poly
        else:
            crc >>= 1
    return crc


def oc_sum16(data):
    """One's complement sum of 16-bit big-endian words (data length even)."""
    s = 0
    for k in range(0, len(data), 2):
        s += (data[k] << 8) | data[k+1]
    while s >> 16:
        s = (s & 0xffff) + (s >> 16)
    return s


def csum_update3(hc, m0, n0, m1, n1, m2, n2):
    """RFC 1624 eqn. 3, identical arithmetic to natgw_pkg::csum_update3."""
    s = ((~hc) & 0xffff) + ((~m0) & 0xffff) + n0 + ((~m1) & 0xffff) + n1 + ((~m2) & 0xffff) + n2
    f = (s & 0xffff) + (s >> 16)
    f = (f & 0xffff) + (f >> 16)
    return (~f) & 0xffff


# ---------------------------------------------------------------------------
# frame parsing and classification

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
    L = len(frame)
    vlan = 0
    tci = 0
    o = 0
    et = _w16(frame, 12)
    if et == 0x8100:
        vlan = 1
        tci = _w16(frame, 14)
        et = _w16(frame, 16)
        o = 4

    ip = 14 + o
    ver_ihl = _b(frame, ip)
    totlen = _w16(frame, ip+2)
    frag = _w16(frame, ip+6)
    ttl = _b(frame, ip+8)
    proto = _b(frame, ip+9)
    sip = _w32(frame, ip+12)
    dip = _w32(frame, ip+16)
    sport = _w16(frame, ip+20)
    dport = _w16(frame, ip+22)
    tcp_flags = _b(frame, ip+33)
    tcp = 1 if proto == 6 else 0

    hdr = bytes(_b(frame, ip+k) for k in range(20))
    csum_ok = oc_sum16(hdr) == 0xffff

    is_ipv4 = et == 0x0800
    hdr_ok = ver_ihl == 0x45 and L >= ip + 20 and totlen >= 20 and ip + totlen <= L
    l3ok = is_ipv4 and hdr_ok and csum_ok

    key = Key(lane=lane, vid=(tci & 0xfff) if vlan else 0, tcp=tcp, sip=sip, dip=dip, sport=sport, dport=dport)

    def p(reason, lookup=False, **kw):
        return Parsed(reason=reason, lookup=lookup, l3ok=l3ok, vlan=vlan, tci=tci, key=key,
                      ttl=ttl, tcp=tcp, ip_off=ip, **kw)

    if bypass:
        return p(RSN_BYPASS)
    if _b(frame, 0) & 1:
        return p(RSN_MCAST)
    if not is_ipv4:
        return p(RSN_NOT_IPV4)
    if not hdr_ok:
        return p(RSN_IP_HDR)
    if not csum_ok:
        return p(RSN_CSUM)
    if frag & 0x3fff:
        return p(RSN_FRAG)
    if ttl <= 1:
        return p(RSN_TTL)
    if proto not in (6, 17):
        return p(RSN_PROTO)
    if (tcp and totlen < 40) or (not tcp and totlen < 28):
        return p(RSN_IP_HDR)
    if tcp and tcp_flags & 0x02:
        return p(RSN_SYN)
    if tcp and tcp_flags & 0x05:
        return p(RSN_FINRST, lookup=True, fin=tcp_flags & 1, rst=(tcp_flags >> 2) & 1)
    return p(RSN_MISS, lookup=True)


# ---------------------------------------------------------------------------
# table geometry and host-side cuckoo manager

class TableFull(Exception):
    pass


class CuckooTable:
    """Host copy of the two-table, 4-slot cuckoo table."""

    SLOTS = 4
    TABLES = 2

    def __init__(self, bucket_w, seed0=0xffffffff, seed1=0xffffffff, max_depth=6):
        self.bucket_w = bucket_w
        self.buckets = 1 << bucket_w
        self.seed0 = seed0
        self.seed1 = seed1
        self.max_depth = max_depth
        self.slots = {}     # idx -> Entry
        self.where = {}     # Key -> idx

    @property
    def idx_w(self):
        return self.bucket_w + 3

    @property
    def size(self):
        return self.TABLES * self.buckets * self.SLOTS

    def hashes(self, key):
        kb = key.pack()
        return key_crc(kb, self.seed0, POLY_CRC32C), key_crc(kb, self.seed1, POLY_CRC32)

    def bucket_of(self, key, t):
        h = self.hashes(key)[t]
        return h & (self.buckets - 1)

    def idx(self, t, bucket, slot):
        return (t << (self.bucket_w + 2)) | (bucket << 2) | slot

    def split_idx(self, idx):
        return idx >> (self.bucket_w + 2), (idx >> 2) & (self.buckets - 1), idx & 3

    def line_of(self, idx):
        """(table, bucket) of an index"""
        t, b, _ = self.split_idx(idx)
        return t, b

    def candidates(self, key):
        for t in range(self.TABLES):
            b = self.bucket_of(key, t)
            for s in range(self.SLOTS):
                yield self.idx(t, b, s)

    def lookup(self, key):
        """Hardware lookup: first valid matching slot, T0 slots 0..3 then T1 slots 0..3."""
        for i in self.candidates(key):
            e = self.slots.get(i)
            if e is not None and e.valid and e.key == key:
                return i, e
        return None, None

    def insert(self, entry):
        """Insert; returns the ordered list of (idx, Entry) writes (relocations first)."""
        key = entry.key
        if key in self.where:
            i = self.where[key]
            self.slots[i] = entry
            return [(i, entry)]

        for i in self.candidates(key):
            if i not in self.slots:
                self._place(i, entry)
                return [(i, entry)]

        # BFS for a relocation path ending in a free slot
        start = list(self.candidates(key))
        prev = {i: None for i in start}
        q = deque((i, 1) for i in start)
        found = None
        while q:
            i, depth = q.popleft()
            occ = self.slots[i]
            t, _, _ = self.split_idx(i)
            alt_t = 1 - t
            ab = self.bucket_of(occ.key, alt_t)
            for s in range(self.SLOTS):
                j = self.idx(alt_t, ab, s)
                if j in prev:
                    continue
                prev[j] = i
                if j not in self.slots:
                    found = j
                    break
                if depth < self.max_depth:
                    q.append((j, depth + 1))
            if found is not None:
                break

        if found is None:
            raise TableFull()

        # path: found <- i_k <- ... <- i_1 (a candidate of the new key)
        path = [found]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        writes = []
        # move occupants toward the free slot, last hop first
        for dst, src in zip(path, path[1:]):
            moved = self.slots[src]
            self._place(dst, moved)
            writes.append((dst, moved))
        self._place(path[-1], entry)
        writes.append((path[-1], entry))
        return writes

    def _place(self, i, entry):
        old = self.slots.get(i)
        if old is not None and self.where.get(old.key) == i:
            del self.where[old.key]
        self.slots[i] = entry
        self.where[entry.key] = i

    def delete(self, key):
        i = self.where.pop(key)
        del self.slots[i]
        return [(i, None)]

    def load(self):
        return len(self.slots) / self.size


class DdrTable(CuckooTable):
    """Host copy of the DDR tier: two tables of 64-byte lines holding two
    entries each; buckets come from the top bits of the hashes (the UltraRAM
    tier uses the low bits). Index = {table, bucket, slot}."""

    SLOTS = 2

    @property
    def idx_w(self):
        return self.bucket_w + 2

    def bucket_of(self, key, t):
        return self.hashes(key)[t] >> (32 - self.bucket_w)

    def idx(self, t, bucket, slot):
        return (t << (self.bucket_w + 1)) | (bucket << 1) | slot

    def split_idx(self, idx):
        return idx >> (self.bucket_w + 1), (idx >> 1) & (self.buckets - 1), idx & 1

    def line(self, t, bucket):
        """line number {table, bucket}: the line's address is base + 64 * line"""
        return (t << self.bucket_w) | bucket


DDR_IDX_FLAG = 0x80000000


# ---------------------------------------------------------------------------
# full shim model

@dataclass
class Output:
    kind: str                 # "fwd" or "punt"
    lane: int                 # egress lane (fwd) or ingress lane (punt)
    data: bytes
    reason: int = RSN_FWD
    hit_idx: int = None
    fin: int = 0
    rst: int = 0


@dataclass
class ShimModel:
    bucket_w: int = 10
    seed0: int = 0xffffffff
    seed1: int = 0xffffffff
    enable: bool = False
    bypass_mask: int = 0xff
    punt_hdr: bool = False
    egress_en: int = 0xff
    table: CuckooTable = None
    nh: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    # DDR tier: present when ddr_bucket_w is set; looked up after an on-chip
    # miss while active (calibrated, enabled, not clearing)
    ddr_bucket_w: int = None
    ddr: DdrTable = None
    ddr_active: bool = False
    ddr_activity: set = field(default_factory=set)

    def __post_init__(self):
        if self.table is None:
            self.table = CuckooTable(self.bucket_w, self.seed0, self.seed1)
        if self.ddr is None and self.ddr_bucket_w is not None:
            self.ddr = DdrTable(self.ddr_bucket_w, self.seed0, self.seed1)

    def lookup(self, key):
        """(hit index, entry): UltraRAM tier, then the DDR tier"""
        hit_idx, e = self.table.lookup(key)
        if e is None and self.ddr is not None and self.ddr_active:
            i, e = self.ddr.lookup(key)
            if e is not None:
                hit_idx = DDR_IDX_FLAG | i
                self.ddr_activity.add(i)
        return hit_idx, e

    def read_activity(self, word):
        """host read-and-clear of 64 activity bits"""
        v = 0
        for b in range(64):
            if word * 64 + b in self.ddr_activity:
                v |= 1 << b
                self.ddr_activity.discard(word * 64 + b)
        return v

    def count(self, lane, reason):
        k = (lane, reason)
        self.stats[k] = self.stats.get(k, 0) + 1

    def punt_header(self, lane, reason, p, h0, hit_idx):
        flags = (1 if p.l3ok else 0) | (4 if p.vlan else 0) | (8 if hit_idx is not None else 0)
        return struct.pack(">HBBBBHII", PUNT_MAGIC, PUNT_VER, reason, lane, flags, p.tci,
                           h0 if p.lookup else 0,
                           hit_idx if hit_idx is not None else 0xffffffff)

    def process(self, lane, frame):
        frame = bytes(frame)
        bypass = (not self.enable) or bool((self.bypass_mask >> lane) & 1)
        p = parse(frame, lane, bypass)

        if p.reason == RSN_BYPASS:
            self.count(lane, RSN_BYPASS)
            return Output("punt", lane, frame, RSN_BYPASS)

        h0 = self.table.hashes(p.key)[0] if p.lookup else 0
        hit_idx, e = (None, None)
        if p.lookup:
            hit_idx, e = self.lookup(p.key)

        reason = p.reason
        if reason == RSN_MISS and e is not None:
            nh = self.nh.get(e.nh_idx)
            if nh is None or not nh.valid or not (self.egress_en >> nh.lane) & 1:
                reason = RSN_NH
            elif nh.vlan != p.vlan:
                reason = RSN_VLAN
            else:
                self.count(lane, RSN_FWD)
                return Output("fwd", nh.lane, self.rewrite(frame, p, e, nh), RSN_FWD, hit_idx)

        self.count(lane, reason)
        data = frame
        if self.punt_hdr:
            data = self.punt_header(lane, reason, p, h0, hit_idx) + frame
        return Output("punt", lane, data, reason, hit_idx, p.fin, p.rst)

    @staticmethod
    def rewrite(frame, p, e, nh):
        f = bytearray(frame)
        ip = p.ip_off
        f[0:6] = nh.dst_mac.to_bytes(6, "big")
        f[6:12] = nh.src_mac.to_bytes(6, "big")
        if p.vlan:
            tci = (p.tci & 0xf000) | (nh.vid & 0xfff)
            f[14:16] = tci.to_bytes(2, "big")

        ttl = f[ip+8]
        proto = f[ip+9]
        nttl = (ttl - 1) & 0xff if e.dec_ttl else ttl
        ip_field = ip + (16 if e.xlate_dst else 12)
        port_field = ip + (22 if e.xlate_dst else 20)
        old_ip = int.from_bytes(f[ip_field:ip_field+4], "big")
        old_port = int.from_bytes(f[port_field:port_field+2], "big")
        new_ip = e.new_ip
        new_port = e.new_port

        hc = int.from_bytes(f[ip+10:ip+12], "big")
        hc = csum_update3(hc, old_ip >> 16, new_ip >> 16, old_ip & 0xffff, new_ip & 0xffff,
                          (ttl << 8) | proto, (nttl << 8) | proto)

        l4c_off = ip + 20 + (16 if p.tcp else 6)
        l4c = int.from_bytes(f[l4c_off:l4c_off+2], "big")
        if p.tcp or l4c != 0:
            l4c = csum_update3(l4c, old_ip >> 16, new_ip >> 16, old_ip & 0xffff, new_ip & 0xffff,
                               old_port, new_port)
            if not p.tcp and l4c == 0:
                l4c = 0xffff

        f[ip+8] = nttl
        f[ip+10:ip+12] = hc.to_bytes(2, "big")
        f[ip_field:ip_field+4] = new_ip.to_bytes(4, "big")
        f[port_field:port_field+2] = new_port.to_bytes(2, "big")
        f[l4c_off:l4c_off+2] = l4c.to_bytes(2, "big")
        return bytes(f)
