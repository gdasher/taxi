#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway parser testbench

"""

import ipaddress
import logging
import os
import random
import sys

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.queue import Queue
from cocotb.triggers import RisingEdge, ReadOnly

from cocotbext.axi import AxiStreamBus, AxiStreamFrame, AxiStreamSource, AxiStreamSink

from scapy.layers.l2 import Ether, Dot1Q, ARP
from scapy.layers.inet import IP, TCP, UDP, ICMP
from scapy.layers.inet6 import IPv6
from scapy.packet import Raw
from scapy.compat import raw

try:
    import natgw_model as nm
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    try:
        import natgw_model as nm
    finally:
        del sys.path[0]


META_FIELDS = [("lookup", 1), ("reason", 8), ("dport", 16), ("sport", 16), ("dip", 32), ("sip", 32),
               ("l4_csum", 16), ("ip_csum", 16), ("ttl", 8), ("tcp", 1), ("l3ok", 1), ("vlan", 1), ("tci", 16)]


def unpack(fields, v):
    out = {}
    pos = 0
    for name, w in fields:
        out[name] = (v >> pos) & ((1 << w) - 1)
        pos += w
    return out


def expected_desc(frame, lane, bypass):
    p = nm.parse(frame, lane, bypass)
    ip = p.ip_off
    meta = dict(
        tci=p.tci, vlan=p.vlan, l3ok=int(p.l3ok), tcp=p.tcp, ttl=p.ttl,
        ip_csum=nm._w16(frame, ip+10),
        l4_csum=nm._w16(frame, ip+20+(16 if p.tcp else 6)),
        sip=p.key.sip, dip=p.key.dip, sport=p.key.sport, dport=p.key.dport,
        reason=p.reason, lookup=int(p.lookup),
    )
    return dict(key=p.key, lookup=int(p.lookup), fin=p.fin, rst=p.rst, len=len(frame), meta=meta)


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.lane = int(dut.LANE.value)

        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.DEBUG)

        cocotb.start_soon(Clock(dut.clk, 4, units="ns").start())

        self.source = AxiStreamSource(AxiStreamBus.from_entity(dut.s_axis), dut.clk, dut.rst)
        self.sink = AxiStreamSink(AxiStreamBus.from_entity(dut.m_axis_hold), dut.clk, dut.rst)

        self.desc_queue = Queue()
        self.desc_pause = None
        dut.m_desc_ready.setimmediatevalue(1)
        dut.cfg_bypass.setimmediatevalue(0)

        self.stall_cycles = 0
        self.beats = 0

        cocotb.start_soon(self._desc_mon())

    def set_pause(self, src=None, snk=None, desc=None):
        self.source.set_pause_generator(src() if src else None)
        self.sink.set_pause_generator(snk() if snk else None)
        self.desc_pause = desc() if desc else None

    async def _desc_mon(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            # drive this cycle's ready, then sample the handshake
            if self.desc_pause is not None:
                dut.m_desc_ready.value = 0 if next(self.desc_pause) else 1
            else:
                dut.m_desc_ready.value = 1
            await ReadOnly()
            if dut.m_desc_valid.value and dut.m_desc_ready.value:
                d = dict(
                    key=nm.Key.unpack(int(dut.m_desc_key.value)),
                    lookup=int(dut.m_desc_lookup.value),
                    fin=int(dut.m_desc_fin.value),
                    rst=int(dut.m_desc_rst.value),
                    len=int(dut.m_desc_len.value),
                    meta=unpack(META_FIELDS, int(dut.m_desc_meta.value)),
                )
                self.desc_queue.put_nowait(d)
            if dut.s_axis.tvalid.value and not dut.s_axis.tready.value:
                self.stall_cycles += 1
            if dut.s_axis.tvalid.value and dut.s_axis.tready.value:
                self.beats += 1

    async def reset(self):
        self.dut.rst.setimmediatevalue(0)
        for _ in range(2):
            await RisingEdge(self.dut.clk)
        self.dut.rst.value = 1
        for _ in range(2):
            await RisingEdge(self.dut.clk)
        self.dut.rst.value = 0
        for _ in range(2):
            await RisingEdge(self.dut.clk)

    async def check_frames(self, frames, bypass=False):
        await RisingEdge(self.dut.clk)
        self.dut.cfg_bypass.value = int(bypass)
        users = []
        for f in frames:
            u = random.getrandbits(49)
            users.append(u)
            await self.source.send(AxiStreamFrame(f, tuser=u))
        for f, u in zip(frames, users):
            rx = await self.sink.recv()
            assert bytes(rx.tdata) == bytes(f), "hold output differs from input"
            tu = rx.tuser if isinstance(rx.tuser, list) else [rx.tuser]
            assert all(x == u for x in tu), "tuser not passed through"
            d = await self.desc_queue.get()
            exp = expected_desc(f, self.lane, bypass)
            assert d == exp, f"descriptor mismatch for frame {bytes(f).hex()}\n got {d}\n exp {exp}"
        assert self.desc_queue.empty()


# ---------------------------------------------------------------------------
# frame generators

DST = '02:00:00:00:00:01'
SRC = '02:00:00:00:00:02'


def rip():
    return str(ipaddress.IPv4Address(random.getrandbits(32)))


def pad(b):
    b = bytes(b)
    return b + bytes(max(0, 60 - len(b)))


def mk(l4="tcp", vlan=False, flags="A", payload=None, ipflags=None, **ipkw):
    pkt = Ether(dst=DST, src=SRC)
    if vlan:
        pkt = pkt / Dot1Q(vlan=random.randrange(4096), prio=random.randrange(8))
    ipkw.setdefault("src", rip())
    ipkw.setdefault("dst", rip())
    ipkw.setdefault("ttl", random.randrange(2, 256))
    if ipflags is not None:
        ipkw["flags"] = ipflags
    pkt = pkt / IP(**ipkw)
    sp, dp = random.randrange(65536), random.randrange(65536)
    if l4 == "tcp":
        pkt = pkt / TCP(sport=sp, dport=dp, flags=flags)
    elif l4 == "udp":
        pkt = pkt / UDP(sport=sp, dport=dp)
    elif l4 == "icmp":
        pkt = pkt / ICMP()
    if payload is None:
        payload = bytes(random.randrange(0, 64))
    pkt = pkt / Raw(payload)
    return pad(raw(pkt))


def patch(f, off, data):
    f = bytearray(f)
    f[off:off+len(data)] = data
    return bytes(f)


def reason_frames():
    """At least one frame per classification outcome, VLAN and untagged."""
    out = []
    for vlan in (False, True):
        o = 4 if vlan else 0
        ip = 14 + o
        out.append(mk("tcp", vlan))                             # miss (lookup)
        out.append(mk("udp", vlan))                             # miss (lookup)
        out.append(mk("udp", vlan, payload=b""))                # minimum UDP
        mc = mk("udp", vlan)
        out.append(patch(mc, 0, b'\x01\x00\x5e\x00\x00\x01'))   # mcast
        out.append(patch(mc, 0, b'\xff' * 6))                   # broadcast
        out.append(mk("tcp", vlan, flags="S"))                  # syn
        out.append(mk("tcp", vlan, flags="SA"))                 # syn+ack
        out.append(mk("tcp", vlan, flags="FA"))                 # fin
        out.append(mk("tcp", vlan, flags="R"))                  # rst
        out.append(mk("tcp", vlan, flags="RA"))                 # rst
        out.append(mk("tcp", vlan, flags="FR"))                 # fin+rst
        out.append(mk("tcp", vlan, flags="SF"))                 # syn+fin -> syn
        out.append(mk("icmp", vlan))                            # proto
        out.append(mk("tcp", vlan, proto=47))                   # proto GRE (bad csum? no: scapy computes)
        out.append(mk("udp", vlan, ttl=1))                      # ttl
        out.append(mk("udp", vlan, ttl=0))                      # ttl
        out.append(mk("udp", vlan, ttl=2))                      # ttl ok
        out.append(mk("udp", vlan, ipflags="MF"))                 # frag MF
        out.append(mk("udp", vlan, frag=100))                   # frag offset
        out.append(mk("udp", vlan, ipflags="DF"))                 # DF is fine
        out.append(mk("udp", vlan, options=[b'\x01\x01\x01\x00']))  # options
        f = mk("tcp", vlan)
        out.append(patch(f, ip+10, bytes([f[ip+10] ^ 0x55, f[ip+11]])))  # bad csum
        out.append(patch(f, ip, b'\x65'))                       # bad version (csum still checked after)
        out.append(patch(f, ip, b'\x44'))                       # ihl 4
        tl = nm._w16(f, ip+2)
        out.append(patch(f, ip+2, (len(f) - ip + 1).to_bytes(2, 'big')))  # totlen past frame
        out.append(patch(f, ip+2, (19).to_bytes(2, 'big')))     # totlen < 20
        # short L4: valid header but total length too small for TCP / UDP
        out.append(mk("tcp", vlan, len=39, payload=b""))
        out.append(mk("udp", vlan, len=27, payload=b""))
        out.append(mk("tcp", vlan, len=40, payload=b""))
        out.append(mk("udp", vlan, len=28, payload=b""))
        # not IPv4
        arp = Ether(dst=DST, src=SRC)
        if vlan:
            arp = arp / Dot1Q(vlan=5)
        out.append(pad(raw(arp / ARP())))
        v6 = Ether(dst=DST, src=SRC)
        if vlan:
            v6 = v6 / Dot1Q(vlan=5)
        out.append(pad(raw(v6 / IPv6() / UDP())))
        del tl
    # double tag and QinQ
    out.append(pad(raw(Ether(dst=DST, src=SRC) / Dot1Q(vlan=5) / Dot1Q(vlan=6) / IP(src=rip(), dst=rip()) / UDP())))
    out.append(pad(raw(Ether(dst=DST, src=SRC, type=0x88a8) / Dot1Q(vlan=6) / IP(src=rip(), dst=rip()) / UDP())))
    # frames shorter than the IP header (runts, no padding)
    f = mk("udp")
    for n in (1, 5, 13, 14, 15, 16, 17, 20, 33, 34, 41, 42, 47, 48):
        out.append(f[:n])
    fv = mk("udp", True)
    for n in (17, 18, 37, 38, 45, 46):
        out.append(fv[:n])
    # jumbo
    out.append(mk("udp", False, payload=bytes(random.getrandbits(8) for _ in range(9000 - 42))))
    out.append(mk("tcp", True, payload=bytes(random.getrandbits(8) for _ in range(9000 - 58))))
    # garbage
    for n in (60, 64, 65, 100):
        out.append(bytes(random.getrandbits(8) for _ in range(n)))
    return out


def random_frame():
    r = random.random()
    vlan = random.random() < 0.3
    if r < 0.5:
        return mk(random.choice(["tcp", "udp"]), vlan, flags=random.choice(["A", "PA", "S", "F", "R", "FA"]),
                  payload=bytes(random.randrange(0, 300)))
    if r < 0.8:
        f = bytearray(mk(random.choice(["tcp", "udp", "icmp"]), vlan,
                         ttl=random.choice([0, 1, 2, 64]),
                         ipflags=random.choice([0, 0, 0, "MF", "DF"]),
                         payload=bytes(random.randrange(0, 200))))
        # occasional single-byte corruption in the first 64 bytes
        if random.random() < 0.3:
            k = random.randrange(min(64, len(f)))
            f[k] ^= 1 << random.randrange(8)
        return bytes(f)
    if r < 0.9:
        return mk("udp", vlan)[:random.randrange(1, 60)]
    return bytes(random.getrandbits(8) for _ in range(random.randrange(1, 200)))


def cycle_pause(p):
    def gen():
        while True:
            yield random.random() < p
    return gen


# ---------------------------------------------------------------------------
# tests

@cocotb.test()
async def run_test_reasons(dut):
    tb = TB(dut)
    await tb.reset()
    random.seed(1)

    frames = reason_frames()
    reasons = {nm.parse(f, tb.lane).reason for f in frames}
    tb.log.info("Reasons covered: %s", sorted(nm.RSN_NAMES[r] for r in reasons))
    for r in (nm.RSN_MISS, nm.RSN_NOT_IPV4, nm.RSN_MCAST, nm.RSN_IP_HDR, nm.RSN_FRAG, nm.RSN_TTL,
              nm.RSN_PROTO, nm.RSN_CSUM, nm.RSN_SYN, nm.RSN_FINRST):
        assert r in reasons, f"generator misses reason {nm.RSN_NAMES[r]}"

    await tb.check_frames(frames)

    tb.log.info("Bypass")
    await tb.check_frames(frames[:20], bypass=True)


@cocotb.test()
async def run_test_backpressure(dut):
    tb = TB(dut)
    await tb.reset()
    random.seed(2)

    frames = reason_frames()
    tb.set_pause(src=cycle_pause(0.3), snk=cycle_pause(0.3), desc=cycle_pause(0.5))
    await tb.check_frames(frames)


@cocotb.test()
async def run_test_throughput(dut):
    tb = TB(dut)
    await tb.reset()
    random.seed(3)

    for size, beats in ((60, 4), (1, 1), (16, 1), (17, 2), (64, 4)):
        n = 200
        frames = [mk("udp")[:size] if size < 60 else mk("udp", payload=bytes(size - 42)) for _ in range(n)]
        frames = [f[:size] for f in frames]
        tb.stall_cycles = 0
        tb.beats = 0
        for f in frames:
            await tb.source.send(AxiStreamFrame(f))
        await tb.source.wait()
        start = None
        # count cycles from first to last accepted beat
        cycles = 0
        for f in frames:
            rx = await tb.sink.recv()
            assert bytes(rx.tdata) == bytes(f)
            d = await tb.desc_queue.get()
            assert d == expected_desc(f, tb.lane, False)
        del start, cycles
        tb.log.info("%d frames of %d bytes: %d beats, %d stall cycles", n, size, tb.beats, tb.stall_cycles)
        assert tb.beats == n * beats
        assert tb.stall_cycles == 0, "parser stalled with all outputs ready"


@cocotb.test()
async def run_test_throughput_cycles(dut):
    """Measure cycles for back-to-back minimum frames with all outputs ready."""
    tb = TB(dut)
    await tb.reset()
    random.seed(4)

    n = 256
    frames = [mk("udp", payload=bytes(18)) for _ in range(n)]
    assert all(len(f) == 60 for f in frames)
    for f in frames:
        await tb.source.send(AxiStreamFrame(f))

    # wait for first beat, then count cycles to the last descriptor
    while not (dut.s_axis.tvalid.value and dut.s_axis.tready.value):
        await RisingEdge(dut.clk)
    cycles = 0
    got = 0
    while got < n:
        await RisingEdge(dut.clk)
        cycles += 1
        while not tb.desc_queue.empty():
            tb.desc_queue.get_nowait()
            got += 1
    tb.log.info("%d back-to-back 60-byte frames: %d cycles (ideal %d)", n, cycles, 4 * n)
    assert cycles <= 4 * n + 8
    for _ in range(n):
        await tb.sink.recv()


@cocotb.test()
async def run_test_random(dut):
    tb = TB(dut)
    await tb.reset()
    random.seed(5)

    frames = [random_frame() for _ in range(3000)]
    hist = {}
    for f in frames:
        r = nm.parse(f, tb.lane).reason
        hist[nm.RSN_NAMES[r]] = hist.get(nm.RSN_NAMES[r], 0) + 1
    tb.log.info("Random mix: %s", hist)

    tb.set_pause(src=cycle_pause(0.1), snk=cycle_pause(0.2), desc=cycle_pause(0.2))
    await tb.check_frames(frames[:1500])
    tb.set_pause()
    await tb.check_frames(frames[1500:])


# cocotb-test

tests_dir = os.path.dirname(__file__)
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'rtl'))
lib_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'lib'))
taxi_src_dir = os.path.abspath(os.path.join(lib_dir, 'taxi', 'src'))


def process_f_files(files):
    lst = {}
    for f in files:
        if f[-2:].lower() == '.f':
            with open(f, 'r') as fp:
                l = fp.read().split()
            for f in process_f_files([os.path.join(os.path.dirname(f), x) for x in l]):
                lst[os.path.basename(f)] = f
        else:
            lst[os.path.basename(f)] = f
    return list(lst.values())


def test_natgw_parser(request):
    dut = "natgw_parser"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(taxi_src_dir, "axis", "rtl", "taxi_axis_if.sv"),
        os.path.join(rtl_dir, f"{dut}.f"),
        os.path.join(tests_dir, f"{toplevel}.sv"),
    ]

    verilog_sources = process_f_files(verilog_sources)

    parameters = {}
    parameters['LANE'] = 5
    parameters['USER_W'] = 49

    extra_env = {f'PARAM_{k}': str(v) for k, v in parameters.items()}

    sim_build = os.path.join(tests_dir, "sim_build",
        request.node.name.replace('[', '-').replace(']', ''))

    cocotb_test.simulator.run(
        simulator="verilator",
        python_search=[tests_dir],
        verilog_sources=verilog_sources,
        toplevel=toplevel,
        module=module,
        parameters=parameters,
        sim_build=sim_build,
        extra_env=extra_env,
    )
