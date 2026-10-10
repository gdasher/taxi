#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim: natgw_ddr testbench

The DDR tier against an AXI memory model (cocotbext-axi AxiRam, with random
stalls on every channel). The lookup engine is replaced by a driver that
feeds results (UltraRAM hits, misses of keys present in DDR, misses of absent
keys, frames that were not looked up) on random lanes; the scoreboard is the
reference model's DdrTable.

"""

import itertools
import logging
import os
import random
import sys

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge
from cocotbext.axi import AxiBus, AxiRam

try:
    import natgw_model as nm
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    try:
        import natgw_model as nm
    finally:
        del sys.path[0]

SEED0, SEED1 = 0x12345678, 0x9abcdef0
FLAG = nm.DDR_IDX_FLAG

# result_t, LSB first
RES_FIELDS = [("hit", 1), ("idx", 32), ("xlate_dst", 1), ("new_ip", 32), ("new_port", 16),
              ("dec_ttl", 1), ("nh_idx", 10), ("nh", 128), ("hash", 32)]


def pack(fields, d):
    v, o = 0, 0
    for name, w in fields:
        v |= (d.get(name, 0) & ((1 << w) - 1)) << o
        o += w
    return v


def unpack(fields, v):
    d, o = {}, 0
    for name, w in fields:
        d[name] = (v >> o) & ((1 << w) - 1)
        o += w
    return d


def rand_key(rng):
    return nm.Key(lane=rng.randrange(8), vid=0, tcp=rng.randrange(2), sip=rng.getrandbits(32),
                  dip=rng.getrandbits(32), sport=rng.getrandbits(16), dport=rng.getrandbits(16))


def hashes(key):
    kb = key.pack()
    return nm.key_crc(kb, SEED0, nm.POLY_CRC32C), nm.key_crc(kb, SEED1, nm.POLY_CRC32)


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.INFO)
        self.bucket_w = int(dut.DDR_BUCKET_W.value)
        self.max_out = int(dut.MAX_OUT.value)
        cocotb.start_soon(Clock(dut.clk, 4, unit="ns").start())
        self.ram = AxiRam(AxiBus.from_prefix(dut, "m_axi"), dut.clk, dut.rst, size=2**16)
        for ifc in (self.ram.write_if, self.ram.read_if):
            ifc.log.setLevel(logging.WARNING)
        self.table = nm.DdrTable(self.bucket_w, SEED0, SEED1)
        self.nh = {}
        self.out = {l: [] for l in range(8)}
        self.stats = {"lookup": 0, "hit": 0, "skip": 0, "overflow": 0, "lane_ar": 0}
        cocotb.start_soon(self._monitor())

        dut.s_valid.value = 0
        dut.s_lane.value = 0
        dut.s_res.value = 0
        dut.s_key.value = 0
        dut.s_h1.value = 0
        dut.s_lookup.value = 0
        dut.ddr_calib.value = 1
        dut.cfg_ddr_en.value = 1
        dut.clear_start.value = 0
        dut.host_valid.value = 0
        dut.host_op.value = 0
        dut.host_idx.value = 0
        dut.host_wdata.value = 0
        dut.nh_wr_valid.value = 0
        dut.nh_wr_idx.value = 0
        dut.nh_wr_data.value = 0
        dut.nh_clear_start.value = 0
        dut.act_valid.value = 0
        dut.act_word.value = 0

    def stall(self, prob):
        """random stalls on every AXI channel"""
        def gen(seed):
            rng = random.Random(seed)
            while True:
                yield rng.random() < prob
        if prob:
            for i, ch in enumerate([self.ram.write_if.aw_channel, self.ram.write_if.w_channel,
                                    self.ram.write_if.b_channel, self.ram.read_if.ar_channel,
                                    self.ram.read_if.r_channel]):
                ch.set_pause_generator(gen(i))
        else:
            for ch in [self.ram.write_if.aw_channel, self.ram.write_if.w_channel,
                       self.ram.write_if.b_channel, self.ram.read_if.ar_channel,
                       self.ram.read_if.r_channel]:
                ch.clear_pause_generator()

    async def _monitor(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.m_valid.value:
                self.out[int(dut.m_lane.value)].append(unpack(RES_FIELDS, int(dut.m_res.value)))
            self.stats["lookup"] += int(dut.stat_lookup.value)
            self.stats["hit"] += int(dut.stat_hit.value)
            self.stats["skip"] += int(dut.stat_skip.value)
            self.stats["overflow"] += int(dut.err_overflow.value)
            if dut.m_axi_arvalid.value and dut.m_axi_arready.value and int(dut.m_axi_arid.value) < 8:
                self.stats["lane_ar"] += 1

    async def reset(self):
        self.dut.rst.value = 1
        for _ in range(10):
            await RisingEdge(self.dut.clk)
        self.dut.rst.value = 0
        for _ in range(10):
            await RisingEdge(self.dut.clk)

    async def clear(self):
        self.dut.clear_start.value = 1
        self.dut.nh_clear_start.value = 1
        await RisingEdge(self.dut.clk)
        self.dut.clear_start.value = 0
        self.dut.nh_clear_start.value = 0
        await RisingEdge(self.dut.clk)
        while self.dut.clear_busy.value:
            await RisingEdge(self.dut.clk)
        for _ in range(1030):       # the next-hop mirror clears 1024 rows
            await RisingEdge(self.dut.clk)

    async def host(self, op, idx, entry=None):
        dut = self.dut
        dut.host_op.value = op
        dut.host_idx.value = idx
        dut.host_wdata.value = entry.pack() if entry else 0
        dut.host_valid.value = 1
        while True:
            await RisingEdge(dut.clk)
            if dut.host_ready.value:
                break
        dut.host_valid.value = 0
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.host_done.value:
                break
        await RisingEdge(dut.clk)
        return int(dut.host_rdata.value) if op == 3 else None

    async def write_entry(self, idx, entry):
        await self.host(1, idx, entry)

    async def insert(self, entry):
        for idx, e in self.table.insert(entry):
            await self.write_entry(idx, e)

    async def set_nh(self, idx, nh):
        self.nh[idx] = nh
        self.dut.nh_wr_valid.value = 1
        self.dut.nh_wr_idx.value = idx
        self.dut.nh_wr_data.value = nh.pack()
        await RisingEdge(self.dut.clk)
        self.dut.nh_wr_valid.value = 0

    async def read_activity(self, word):
        dut = self.dut
        dut.act_word.value = word
        dut.act_valid.value = 1
        while True:
            await RisingEdge(dut.clk)
            if dut.act_ready.value:
                break
        dut.act_valid.value = 0
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.act_rvalid.value:
                v = int(dut.act_rdata.value)
                break
        await RisingEdge(dut.clk)
        return v

    async def drive(self, items, gap_prob=0.3, rng=None):
        """items: (lane, res dict, key, h1, lookup); returns once all are sent"""
        dut = self.dut
        rng = rng or random.Random(1)
        for lane, res, key, h1, lookup in items:
            while rng.random() < gap_prob:
                dut.s_valid.value = 0
                await RisingEdge(dut.clk)
            dut.s_valid.value = 1
            dut.s_lane.value = lane
            dut.s_res.value = pack(RES_FIELDS, res)
            dut.s_key.value = key.pack()
            dut.s_h1.value = h1
            dut.s_lookup.value = lookup
            await RisingEdge(dut.clk)
        dut.s_valid.value = 0

    async def settle(self, expect):
        for _ in range(20000):
            await RisingEdge(self.dut.clk)
            if sum(len(v) for v in self.out.values()) >= expect:
                break
        for _ in range(50):
            await RisingEdge(self.dut.clk)


async def populate(tb, rng, n_keys, n_nh=8):
    for i in range(1, n_nh + 1):
        await tb.set_nh(i, nm.NextHop(dst_mac=rng.getrandbits(48), src_mac=rng.getrandbits(48),
                                      lane=rng.randrange(8), valid=1))
    keys = []
    for _ in range(n_keys):
        k = rand_key(rng)
        e = nm.Entry(key=k, xlate_dst=rng.randrange(2), new_ip=rng.getrandbits(32),
                     new_port=rng.getrandbits(16), dec_ttl=rng.randrange(2), nh_idx=rng.randrange(1, n_nh + 1))
        try:
            await tb.insert(e)
            keys.append(k)
        except nm.TableFull:
            break
    return keys


def make_items(rng, present, n, absent_frac=0.15, uram_frac=0.2, nolookup_frac=0.3):
    items, expect = [], {l: [] for l in range(8)}
    for _ in range(n):
        lane = rng.randrange(8)
        r = rng.random()
        if r < nolookup_frac:
            res = {"hit": 0, "hash": 0, "nh": 0}
            key, h1, lookup, kind = rand_key(rng), rng.getrandbits(32), 0, "nolookup"
        elif r < nolookup_frac + uram_frac:
            key = rand_key(rng)
            h0, h1 = hashes(key)
            res = {"hit": 1, "idx": rng.getrandbits(19), "xlate_dst": 1, "new_ip": rng.getrandbits(32),
                   "new_port": rng.getrandbits(16), "dec_ttl": 1, "nh_idx": 3, "nh": rng.getrandbits(113),
                   "hash": h0}
            lookup, kind = 1, "uram"
        else:
            key = rand_key(rng) if (rng.random() < absent_frac / (1 - nolookup_frac - uram_frac)
                                    or not present) else rng.choice(present)
            h0, h1 = hashes(key)
            res = {"hit": 0, "hash": h0}
            lookup, kind = 1, "miss"
        items.append((lane, res, key, h1, lookup))
        expect[lane].append((kind, res, key))
    return items, expect


def check(tb, expect, allow_skips):
    """per lane, in order: unchanged results pass through, DDR hits carry the
    entry; a miss that was skipped (request cap) stays a miss"""
    hits = present_missed = misses = 0
    for lane in range(8):
        got = tb.out[lane]
        assert len(got) == len(expect[lane]), f"lane {lane}: {len(got)} results, expected {len(expect[lane])}"
        for n, ((kind, res, key), g) in enumerate(zip(expect[lane], got)):
            want = dict({f: 0 for f, _ in RES_FIELDS}, **res)
            if kind != "miss":
                assert g == want, f"lane {lane} result {n} ({kind}) changed: {g} != {want}"
                continue
            misses += 1
            idx, e = tb.table.lookup(key)
            if e is not None and g["hit"]:
                hits += 1
                nh = tb.nh[e.nh_idx]
                want.update(hit=1, idx=FLAG | idx, xlate_dst=e.xlate_dst, new_ip=e.new_ip, new_port=e.new_port,
                            dec_ttl=e.dec_ttl, nh_idx=e.nh_idx, nh=nh.pack())
                assert g == want, f"lane {lane} result {n}: DDR hit {g} != {want}"
            else:
                if e is not None:
                    present_missed += 1
                assert g == want, f"lane {lane} result {n}: miss changed: {g} != {want}"
    assert tb.stats["hit"] == hits
    assert tb.stats["lookup"] + tb.stats["skip"] == misses, "every miss is looked up or skipped"
    assert present_missed <= tb.stats["skip"], "a present key missed without a skip"
    if not allow_skips:
        assert tb.stats["skip"] == 0 and present_missed == 0
    assert tb.stats["overflow"] == 0
    return hits


@cocotb.test()
async def run_test_host_access(dut):
    tb = TB(dut)
    await tb.reset()
    await tb.clear()
    rng = random.Random(3)
    size = 2 * (1 << tb.bucket_w) * 2
    written = {}
    for _ in range(60):
        idx = rng.randrange(size)
        e = nm.Entry(key=rand_key(rng), new_ip=rng.getrandbits(32), nh_idx=rng.randrange(1024))
        await tb.write_entry(idx, e)
        written[idx] = e
    for idx, e in written.items():
        assert await tb.host(3, idx) == e.pack(), f"read back {idx}"
    idx = next(iter(written))
    await tb.host(2, idx)
    assert await tb.host(3, idx) == 0
    # the neighbouring slot of the same line is untouched by the clear
    other = idx ^ 1
    if other in written:
        assert await tb.host(3, other) == written[other].pack()
    await tb.clear()
    assert tb.ram.read(0, size // 2 * 64) == bytes(size // 2 * 64), "bulk clear left data"
    for idx in list(written)[:10]:
        assert await tb.host(3, idx) == 0


async def lookup_test(dut, stall, n, allow_skips, seed, gap=0.3):
    tb = TB(dut)
    await tb.reset()
    await tb.clear()
    rng = random.Random(seed)
    present = await populate(tb, rng, 2 * (1 << tb.bucket_w) * 2)
    tb.log.info("DDR table: %d entries (load %.2f)", len(present), tb.table.load())
    tb.stall(stall)
    items, expect = make_items(rng, present, n)
    await tb.drive(items, gap_prob=gap, rng=rng)
    await tb.settle(n)
    hits = check(tb, expect, allow_skips)
    tb.log.info("hits %d, lookups %d, skips %d", hits, tb.stats["lookup"], tb.stats["skip"])
    return tb, hits


@cocotb.test()
async def run_test_lookup_exact(dut):
    """at a rate the cap never limits, every present key hits"""
    tb, hits = await lookup_test(dut, stall=0.0, n=2000, allow_skips=False, seed=11, gap=0.9)
    assert hits > 300


@cocotb.test()
async def run_test_lookup(dut):
    """back to back, random lanes"""
    tb, hits = await lookup_test(dut, stall=0.0, n=3000, allow_skips=True, seed=16, gap=0.0)
    assert hits > 500


@cocotb.test()
async def run_test_lookup_stalls(dut):
    """random stalls on every AXI channel: still exact, in order"""
    tb, hits = await lookup_test(dut, stall=0.3, n=3000, allow_skips=True, seed=12, gap=0.0)
    assert hits > 300


@cocotb.test()
async def run_test_request_cap(dut):
    """back-to-back misses with a slow memory: past the cap, frames are punted
    as misses (skips), never stalled, never reordered"""
    tb, hits = await lookup_test(dut, stall=0.8, n=2000, allow_skips=True, seed=13, gap=0.0)
    assert tb.stats["skip"] > 0, "the request cap was never reached"


@cocotb.test()
async def run_test_activity(dut):
    tb, hits = await lookup_test(dut, stall=0.0, n=1500, allow_skips=True, seed=14, gap=0.0)
    want = set()
    for lane in range(8):
        for g in tb.out[lane]:
            if g["hit"] and g["idx"] & FLAG:
                want.add(g["idx"] & ~FLAG)
    words = (2 * (1 << tb.bucket_w) * 2 + 63) // 64
    got = set()
    for w in range(words):
        v = await tb.read_activity(w)
        got |= {w * 64 + b for b in range(64) if (v >> b) & 1}
    assert got == want, f"activity {sorted(got ^ want)[:10]} differ"
    for w in range(words):
        assert await tb.read_activity(w) == 0, "read did not clear"


@cocotb.test()
async def run_test_inactive(dut):
    """not calibrated (no DIMM), or not enabled: no DDR read, results unchanged"""
    for calib, en in ((0, 1), (1, 0)):
        tb = TB(dut)
        await tb.reset()
        await tb.clear()
        rng = random.Random(15)
        present = await populate(tb, rng, 100)
        dut.ddr_calib.value = calib
        dut.cfg_ddr_en.value = en
        await RisingEdge(dut.clk)
        assert not dut.ddr_active.value
        items, expect = make_items(rng, present, 800)
        await tb.drive(items, rng=rng)
        await tb.settle(800)
        for lane in range(8):
            assert [g for g in tb.out[lane]] == [dict({f: 0 for f, _ in RES_FIELDS}, **res)
                                                 for _, res, _ in expect[lane]]
        assert tb.stats["lane_ar"] == 0 and tb.stats["lookup"] == 0 and tb.stats["hit"] == 0


# cocotb-test

tests_dir = os.path.dirname(__file__)
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'rtl'))


@pytest.mark.parametrize("act_pipe", [2, 3])
def test_natgw_ddr(request, act_pipe):
    dut = "natgw_ddr"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [os.path.join(rtl_dir, f) for f in
                       ("natgw_pkg.sv", "natgw_fifo.sv", "natgw_ram.sv", "natgw_actmap.sv", f"{dut}.sv")]
    verilog_sources.append(os.path.join(tests_dir, f"{toplevel}.sv"))

    parameters = {'DDR_BUCKET_W': 6, 'MAX_OUT': 4, 'QUEUE_DEPTH': 64, 'ACT_PIPE': act_pipe}
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
