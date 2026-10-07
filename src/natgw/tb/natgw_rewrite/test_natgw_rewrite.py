#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim: natgw_rewrite testbench

Results and metadata are built from natgw_model (parse() and a CuckooTable
plus next-hop dict, as the parser and lookup engine would produce them);
expected outputs come from ShimModel.process().

"""

import ipaddress
import itertools
import logging
import os
import random
import sys

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.queue import Queue
from cocotb.triggers import RisingEdge
from cocotb.utils import get_sim_time

from cocotbext.axi import AxiStreamBus, AxiStreamFrame, AxiStreamSource, AxiStreamSink

from scapy.all import Ether, IP, TCP, UDP, ICMP, ARP, Dot1Q, IPv6, raw
from scapy.layers.inet import IPOption

try:
    import natgw_model as nm
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    try:
        import natgw_model as nm
    finally:
        del sys.path[0]


RES_FIELDS = [("hit", 1), ("idx", 32), ("xlate_dst", 1), ("new_ip", 32), ("new_port", 16),
              ("dec_ttl", 1), ("nh_idx", 10), ("nh", 128), ("hash", 32)]
META_FIELDS = [("lookup", 1), ("reason", 8), ("dport", 16), ("sport", 16), ("dip", 32), ("sip", 32),
               ("l4_csum", 16), ("ip_csum", 16), ("ttl", 8), ("tcp", 1), ("l3ok", 1), ("vlan", 1), ("tci", 16)]

CLK_NS = 4
EGRESS_EN = 0x7f   # lane 7 disabled


def w16(f, i):
    return (nm._b(f, i) << 8) | nm._b(f, i+1)


def make_meta(frame, p):
    ip = p.ip_off
    return nm._pack(META_FIELDS, dict(
        lookup=int(p.lookup), reason=p.reason,
        dport=p.key.dport, sport=p.key.sport, dip=p.key.dip, sip=p.key.sip,
        l4_csum=w16(frame, ip + 20 + (16 if p.tcp else 6)), ip_csum=w16(frame, ip + 10),
        ttl=p.ttl, tcp=p.tcp, l3ok=int(p.l3ok), vlan=p.vlan, tci=p.tci))


def make_result(model, p):
    if not p.lookup:
        return 0
    h0 = model.table.hashes(p.key)[0]
    idx, e = model.table.lookup(p.key)
    if e is None:
        return nm._pack(RES_FIELDS, dict(hash=h0))
    nh = model.nh.get(e.nh_idx)
    return nm._pack(RES_FIELDS, dict(
        hit=1, idx=idx, xlate_dst=e.xlate_dst, new_ip=e.new_ip, new_port=e.new_port,
        dec_ttl=e.dec_ttl, nh_idx=e.nh_idx, nh=nh.pack() if nh else 0, hash=h0))


class TB:
    def __init__(self, dut):
        self.dut = dut

        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.INFO)

        cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())

        self.source = AxiStreamSource(AxiStreamBus.from_entity(dut.s_axis_hold), dut.clk, dut.rst)
        self.fwd_sink = AxiStreamSink(AxiStreamBus.from_entity(dut.m_axis_fwd), dut.clk, dut.rst)
        self.punt_sink = AxiStreamSink(AxiStreamBus.from_entity(dut.m_axis_punt), dut.clk, dut.rst)

        self.res_queue = Queue()
        self.meta_queue = Queue()
        self.res_pause = None
        self.meta_pause = None

        self.stats = []
        self.first_in = None

        dut.s_res_valid.setimmediatevalue(0)
        dut.s_res.setimmediatevalue(0)
        dut.s_meta_valid.setimmediatevalue(0)
        dut.s_meta.setimmediatevalue(0)
        dut.cfg_punt_hdr.setimmediatevalue(0)
        dut.cfg_egress_en.setimmediatevalue(EGRESS_EN)

        cocotb.start_soon(self._drive(dut.s_res_valid, dut.s_res_ready, dut.s_res, self.res_queue, "res_pause"))
        cocotb.start_soon(self._drive(dut.s_meta_valid, dut.s_meta_ready, dut.s_meta, self.meta_queue, "meta_pause"))
        cocotb.start_soon(self._stat_mon())

    async def _drive(self, valid, ready, data, queue, pause_attr):
        while True:
            item = await queue.get()
            pause = getattr(self, pause_attr)
            if pause is not None:
                while next(pause):
                    valid.value = 0
                    await RisingEdge(self.dut.clk)
            data.value = item
            valid.value = 1
            await RisingEdge(self.dut.clk)
            while not ready.value:
                await RisingEdge(self.dut.clk)
            valid.value = 0

    async def _stat_mon(self):
        while True:
            await RisingEdge(self.dut.clk)
            if self.dut.stat_valid.value:
                self.stats.append(int(self.dut.stat_reason.value))

    def set_idle_generator(self, generator=None):
        if generator:
            self.source.set_pause_generator(generator())
            self.res_pause = generator()
            self.meta_pause = generator()
        else:
            self.source.set_pause_generator(None)
            self.res_pause = None
            self.meta_pause = None

    def set_backpressure_generator(self, generator=None):
        if generator:
            self.fwd_sink.set_pause_generator(generator())
            self.punt_sink.set_pause_generator(generator())
        else:
            self.fwd_sink.set_pause_generator(None)
            self.punt_sink.set_pause_generator(None)

    async def reset(self):
        self.dut.rst.setimmediatevalue(0)
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 1
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 0
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)


# ---------------------------------------------------------------------------
# frame construction

def rip():
    return str(ipaddress.IPv4Address(random.getrandbits(32)))


def l2(vlan=None, dst='02:00:00:00:00:01'):
    pkt = Ether(dst=dst, src='02:00:00:00:00:02')
    if vlan is not None:
        pkt = pkt/Dot1Q(vlan=vlan, prio=5)
    return pkt


def mk(tcp=True, vlan=None, flags='A', ttl=64, payload=None, **ipkw):
    if payload is None:
        payload = bytes(random.randrange(256) for _ in range(random.randrange(0, 80)))
    l4 = (TCP(sport=random.randrange(1, 65536), dport=random.randrange(1, 65536), flags=flags) if tcp
          else UDP(sport=random.randrange(1, 65536), dport=random.randrange(1, 65536)))
    ipkw.setdefault('src', rip())
    ipkw.setdefault('dst', rip())
    return raw(l2(vlan)/IP(ttl=ttl, **ipkw)/l4/payload)


def set_bytes(f, off, val):
    f = bytearray(f)
    f[off:off+len(val)] = val
    return bytes(f)


class Scenario:
    """Model plus helpers to install entries and next hops."""

    def __init__(self, lane, punt_hdr):
        self.lane = lane
        self.model = nm.ShimModel(bucket_w=8, enable=True, bypass_mask=0, punt_hdr=punt_hdr, egress_en=EGRESS_EN)
        nhs = {
            1: nm.NextHop(dst_mac=0x020000000a01, src_mac=0x020000000b01, lane=1),
            2: nm.NextHop(dst_mac=0x020000000a02, src_mac=0x020000000b02, lane=5),
            3: nm.NextHop(dst_mac=0x020000000a03, src_mac=0x020000000b03, lane=3, vlan=1, vid=100),
            4: nm.NextHop(dst_mac=0x020000000a04, src_mac=0x020000000b04, lane=0, vlan=1, vid=4095),
            5: nm.NextHop(dst_mac=0x020000000a05, src_mac=0x020000000b05, lane=2, valid=0),
            6: nm.NextHop(dst_mac=0x020000000a06, src_mac=0x020000000b06, lane=7),
            7: nm.NextHop(dst_mac=0x020000000a07, src_mac=0x020000000b07, lane=6, vlan=1, vid=7),
        }
        self.model.nh.update(nhs)

    def install(self, frame, nh_idx=None, xlate_dst=None, dec_ttl=None, new_port=None, new_ip=None):
        p = nm.parse(frame, self.lane)
        if nh_idx is None:
            nh_idx = random.choice([3, 4, 7]) if p.vlan else random.choice([1, 2])
        e = nm.Entry(key=p.key,
                     xlate_dst=random.randrange(2) if xlate_dst is None else xlate_dst,
                     new_ip=random.getrandbits(32) if new_ip is None else new_ip,
                     new_port=random.randrange(65536) if new_port is None else new_port,
                     dec_ttl=random.randrange(2) if dec_ttl is None else dec_ttl,
                     nh_idx=nh_idx)
        self.model.table.insert(e)
        return e


def directed_frames(sc):
    """(frame, bypass) pairs covering every decision path."""
    fr = []

    # forwarded: TCP/UDP x VLAN/untagged x SNAT/DNAT x dec_ttl
    for tcp, vlan, xd, dt in itertools.product([True, False], [None, 42], [0, 1], [0, 1]):
        f = mk(tcp=tcp, vlan=vlan)
        sc.install(f, xlate_dst=xd, dec_ttl=dt)
        fr.append((f, False))

    # minimum-size frames (partial last beat)
    for tcp, vlan in itertools.product([True, False], [None, 9]):
        f = mk(tcp=tcp, vlan=vlan, payload=b'')
        sc.install(f)
        fr.append((f, False))

    # UDP zero checksum, untagged and tagged
    for vlan in [None, 5]:
        f = mk(tcp=False, vlan=vlan)
        o = 14 + (4 if vlan is not None else 0)
        f = set_bytes(f, o + 26, b'\0\0')
        sc.install(f)
        fr.append((f, False))

    # UDP update producing 0x0000 -> must be sent as 0xFFFF
    for vlan in [None, 11]:
        while True:
            f = mk(tcp=False, vlan=vlan)
            p = nm.parse(f, sc.lane)
            o = p.ip_off
            c = w16(f, o + 26)
            if c == 0:
                continue
            new_ip = random.getrandbits(32)
            old_ip = p.key.sip
            old_port = p.key.sport
            hit = None
            for port in range(65536):
                if nm.csum_update3(c, old_ip >> 16, new_ip >> 16, old_ip & 0xffff, new_ip & 0xffff,
                                   old_port, port) == 0:
                    hit = port
                    break
            if hit is not None:
                break
        sc.install(f, xlate_dst=0, new_ip=new_ip, new_port=hit)
        fr.append((f, False))

    # next hop invalid, lane disabled, missing next hop, VLAN mismatch both ways
    f = mk(); sc.install(f, nh_idx=5); fr.append((f, False))
    f = mk(); sc.install(f, nh_idx=6); fr.append((f, False))
    f = mk(); sc.install(f, nh_idx=1000); fr.append((f, False))
    f = mk(); sc.install(f, nh_idx=3); fr.append((f, False))
    f = mk(vlan=3); sc.install(f, nh_idx=1); fr.append((f, False))

    # miss
    fr.append((mk(), False))
    fr.append((mk(tcp=False, vlan=8), False))

    # FIN/RST with hit and miss; SYN
    f = mk(flags='FA'); sc.install(f); fr.append((f, False))
    f = mk(flags='R', vlan=4); sc.install(f); fr.append((f, False))
    fr.append((mk(flags='FA'), False))
    fr.append((mk(flags='RA'), False))
    f = mk(flags='S'); sc.install(f); fr.append((f, False))
    fr.append((mk(flags='SA'), False))

    # exceptions
    fr.append((raw(Ether(dst='ff:ff:ff:ff:ff:ff')/ARP()), False))
    fr.append((raw(Ether(dst='02:00:00:00:00:01')/ARP(op=2)), False))
    fr.append((raw(Ether(dst='01:00:5e:00:00:01', src='02:00:00:00:00:02')/IP(dst='224.0.0.1')/UDP()/b'abc'), False))
    fr.append((raw(Ether(dst='02:00:00:00:00:01')/IPv6()/UDP()/b'v6'), False))
    fr.append((raw(Ether(dst='02:00:00:00:00:01')/Dot1Q(vlan=1)/Dot1Q(vlan=2)/IP()/UDP()), False))
    fr.append((raw(l2()/IP(options=[IPOption(b'\x01\x01\x01\x01')])/TCP()/b'opt'), False))
    f = mk(); p = nm.parse(f, 0); fr.append((set_bytes(f, p.ip_off + 2, (len(f) - 13).to_bytes(2, 'big')), False))
    fr.append((raw(l2()/IP(len=30)/TCP(flags='A')), False))
    f = mk(); fr.append((set_bytes(f, 14 + 10, b'\x12\x34'), False))
    fr.append((raw(l2()/IP(flags='MF')/UDP()/b'frag'), False))
    fr.append((raw(l2(vlan=6)/IP(frag=100)/UDP()/b'frag'), False))
    for t in [0, 1]:
        f = mk(ttl=t); sc.install(f); fr.append((f, False))
    fr.append((raw(l2()/IP()/ICMP()/b'ping'), False))

    # bypass (also with an installed entry)
    f = mk(); sc.install(f); fr.append((f, True))
    fr.append((mk(vlan=12), True))

    return fr


def random_frame(sc):
    r = random.random()
    vlan = random.choice([None, None, random.randrange(4096)])
    tcp = random.random() < 0.6
    if r < 0.55:
        f = mk(tcp=tcp, vlan=vlan)
        sc.install(f, nh_idx=random.choice([1, 2, 3, 4, 5, 6, 7]) if random.random() < 0.15 else None)
        return f, False
    if r < 0.7:
        return mk(tcp=tcp, vlan=vlan), False
    if r < 0.8:
        f = mk(flags=random.choice(['FA', 'RA', 'S', 'SA', 'R']), vlan=vlan)
        if random.random() < 0.5:
            sc.install(f)
        return f, False
    if r < 0.9:
        return random.choice([
            raw(Ether(dst='ff:ff:ff:ff:ff:ff')/ARP()),
            raw(l2(vlan)/IP(ttl=random.randrange(2))/UDP()/b'ttl'),
            raw(l2(vlan)/IP()/ICMP()),
            raw(l2(vlan)/IP(flags='MF')/TCP()),
            set_bytes(mk(vlan=vlan), 14 + (4 if vlan is not None else 0) + 10, b'\xde\xad'),
        ]), False
    return mk(tcp=tcp, vlan=vlan), True


# ---------------------------------------------------------------------------

async def run_frames(tb, sc, frames):
    model = sc.model
    exp_fwd = []
    exp_punt = []
    exp_stats = []
    tb.stats.clear()

    for frame, bypass in frames:
        model.bypass_mask = 0xff if bypass else 0
        p = nm.parse(frame, sc.lane, bypass)
        meta = make_meta(frame, p)
        res = make_result(model, p)
        out = model.process(sc.lane, frame)
        tuser = random.getrandbits(48) << 1

        if out.kind == "fwd":
            exp_fwd.append(out)
        else:
            exp_punt.append((out, tuser))
        exp_stats.append(out.reason)

        await tb.res_queue.put(res)
        await tb.meta_queue.put(meta)
        await tb.source.send(AxiStreamFrame(frame, tuser=tuser))

    for out in exp_fwd:
        rx = await tb.fwd_sink.recv()
        assert bytes(rx.tdata) == out.data, f"fwd mismatch\n{bytes(rx.tdata).hex()}\n{out.data.hex()}"
        assert rx.tdest == out.lane
        assert not rx.tuser
        pkt = Ether(bytes(rx.tdata))
        ipl = pkt[IP]
        c = ipl.chksum
        del ipl.chksum
        assert Ether(raw(pkt))[IP].chksum == c
        assert nm.oc_sum16(bytes(rx.tdata)[(18 if Dot1Q in pkt else 14):][:20]) == 0xffff

    for out, tuser in exp_punt:
        rx = await tb.punt_sink.recv()
        assert bytes(rx.tdata) == out.data, \
            f"punt mismatch reason {nm.RSN_NAMES[out.reason]}\n{bytes(rx.tdata).hex()}\n{out.data.hex()}"
        assert rx.tuser == tuser

    tb.t_last_rx = get_sim_time('ns')

    for k in range(10):
        await RisingEdge(tb.dut.clk)

    assert tb.fwd_sink.empty()
    assert tb.punt_sink.empty()
    assert tb.stats == exp_stats

    return len(exp_fwd), len(exp_punt)


def cycle_pause():
    return itertools.cycle([1, 1, 1, 0])


def random_pause():
    rng = random.Random(7)
    while True:
        yield rng.random() < 0.3


@cocotb.test()
@cocotb.parametrize(
    ("punt_hdr", [0, 1]),
    ("idle_inserter", [None, cycle_pause, random_pause]),
    ("backpressure_inserter", [None, cycle_pause, random_pause]),
)
async def run_test_directed(dut, punt_hdr=0, idle_inserter=None, backpressure_inserter=None):
    tb = TB(dut)
    random.seed(1000 + punt_hdr)
    await tb.reset()
    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)
    dut.cfg_punt_hdr.value = punt_hdr

    sc = Scenario(int(dut.LANE.value), punt_hdr)
    frames = directed_frames(sc)
    nf, np_ = await run_frames(tb, sc, frames)
    reasons = sorted(set(tb.stats))
    tb.log.info("directed: %d forwarded, %d punted, reasons %s", nf, np_, [nm.RSN_NAMES[r] for r in reasons])
    expected = {nm.RSN_FWD, nm.RSN_MISS, nm.RSN_BYPASS, nm.RSN_NOT_IPV4, nm.RSN_MCAST, nm.RSN_IP_HDR,
                nm.RSN_FRAG, nm.RSN_TTL, nm.RSN_PROTO, nm.RSN_CSUM, nm.RSN_SYN, nm.RSN_FINRST,
                nm.RSN_VLAN, nm.RSN_NH}
    assert set(tb.stats) == expected


@cocotb.test()
@cocotb.parametrize(
    ("punt_hdr", [0, 1]),
    ("idle_inserter", [None, random_pause]),
    ("backpressure_inserter", [None, random_pause]),
)
async def run_test_random(dut, punt_hdr=0, idle_inserter=None, backpressure_inserter=None):
    tb = TB(dut)
    random.seed(2000 + punt_hdr)
    await tb.reset()
    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)
    dut.cfg_punt_hdr.value = punt_hdr

    sc = Scenario(int(dut.LANE.value), punt_hdr)
    frames = [random_frame(sc) for _ in range(1500)]
    nf, np_ = await run_frames(tb, sc, frames)
    tb.log.info("random: %d forwarded, %d punted", nf, np_)


@cocotb.test()
@cocotb.parametrize(("kind", ["fwd", "punt", "punt_hdr"]))
async def run_test_throughput(dut, kind="fwd"):
    """Back-to-back 60-byte frames: forwarding and plain punts must run at one beat per clock."""
    tb = TB(dut)
    random.seed(3000)
    await tb.reset()
    dut.cfg_punt_hdr.value = int(kind == "punt_hdr")

    sc = Scenario(int(dut.LANE.value), kind == "punt_hdr")
    count = 200
    frames = []
    for _ in range(count):
        f = mk(tcp=True, payload=b'')
        f = f + bytes(60 - len(f))     # pad to 60 bytes like the MAC does
        if kind == "fwd":
            sc.install(f, nh_idx=1)
        frames.append((f, False))

    # preload result/meta so only the frame stream paces the block
    tb.source.pause = True
    run = cocotb.start_soon(run_frames(tb, sc, frames))
    for _ in range(50):
        await RisingEdge(dut.clk)

    t_start = get_sim_time('ns')
    tb.source.pause = False

    await run
    beats = count * (4 + (1 if kind == "punt_hdr" else 0))
    cycles = (tb.t_last_rx - t_start) / CLK_NS
    tb.log.info("throughput %s: %d frames, %d beats in %.0f cycles (%.3f beats/cycle)",
                kind, count, beats, cycles, beats / cycles)
    assert cycles < beats + 40


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


def test_natgw_rewrite(request):
    dut = "natgw_rewrite"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(rtl_dir, "natgw_pkg.sv"),
        os.path.join(tests_dir, f"{toplevel}.sv"),
        os.path.join(rtl_dir, f"{dut}.f"),
    ]

    verilog_sources = process_f_files(verilog_sources)

    parameters = {}

    parameters['LANE'] = 2
    parameters['USER_W'] = 49

    extra_env = {f'PARAM_{k}': str(v) for k, v in parameters.items()}

    sim_build = os.path.join(tests_dir, "sim_build",
        request.node.name.replace('[', '-').replace(']', ''))

    cocotb_test.simulator.run(
        simulator="verilator",
        python_search=[tests_dir, os.path.join(tests_dir, '..')],
        verilog_sources=verilog_sources,
        toplevel=toplevel,
        module=module,
        parameters=parameters,
        sim_build=sim_build,
        extra_env=extra_env,
    )
