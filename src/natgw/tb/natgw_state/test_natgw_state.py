#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim: natgw_state testbench

"""

import logging
import os
import random
import sys

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

try:
    from natgw_model import State, EVT_IDLE, EVT_FIN, EVT_RST, EVT_OVF
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    try:
        from natgw_model import State, EVT_IDLE, EVT_FIN, EVT_RST, EVT_OVF
    finally:
        del sys.path[0]


M32 = 0xffffffff


class StateModel:
    """Op-level model of natgw_state: ops applied in issue order."""

    def __init__(self, idx_w):
        self.st = [State() for _ in range(2**idx_w)]

    def hit(self, idx, length, fin, rst, tick):
        s = self.st[idx]
        s.ts = tick
        s.pkts = (s.pkts + 1) & ((1 << 48) - 1)
        s.bytes = (s.bytes + length) & ((1 << 56) - 1)
        s.evp = 0
        s.fin |= fin
        s.rst |= rst
        if fin:
            return EVT_FIN
        if rst:
            return EVT_RST
        return None

    def write(self, idx, state):
        self.st[idx] = State(**state.__dict__)

    def read(self, idx):
        return State(**self.st[idx].__dict__)

    def clear(self):
        for i in range(len(self.st)):
            self.st[i] = State()

    @staticmethod
    def is_idle(s, tick, th_tcp, th_udp):
        return s.valid and not s.evp and ((tick - s.ts) & M32) > (th_tcp if s.tcp else th_udp)


def decode_evt(e):
    return (e >> 60) & 0xf, (e >> 32) & 0xffffff, e & M32


class TB:
    def __init__(self, dut):
        self.dut = dut

        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.DEBUG)

        self.idx_w = int(dut.IDX_W.value)
        self.ram_pipe = int(dut.RAM_PIPE.value)
        self.evt_depth = int(dut.EVT_DEPTH.value)
        self.n = 2**self.idx_w

        self.model = StateModel(self.idx_w)

        self.cycle = 0
        self.tick = 0
        self.tick_fn = None
        self.evt_ready = True

        self.rdata = []
        self.events = []      # (cycle, type, idx, tick)
        self.drops = 0

        cocotb.start_soon(Clock(dut.clk, 4, units="ns").start())

        for sig in [dut.s_hit_valid, dut.s_hit_idx, dut.s_hit_len, dut.s_hit_fin, dut.s_hit_rst,
                    dut.host_st_valid, dut.host_st_we, dut.host_st_idx, dut.host_st_wdata,
                    dut.cfg_tick, dut.cfg_scan_en, dut.clear_start]:
            sig.setimmediatevalue(0)
        dut.m_evt_ready.setimmediatevalue(1)
        dut.cfg_thresh_tcp.setimmediatevalue(M32)
        dut.cfg_thresh_udp.setimmediatevalue(M32)
        dut.cfg_scan_interval.setimmediatevalue(1)

    async def reset(self):
        self.dut.rst.setimmediatevalue(0)
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 1
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 0
        await RisingEdge(self.dut.clk)
        cocotb.start_soon(self._monitor())

    async def _monitor(self):
        dut = self.dut
        cyc = self.cycle
        while True:
            await RisingEdge(dut.clk)
            if dut.host_st_rvalid.value:
                self.rdata.append(State.unpack(int(dut.host_st_rdata.value)))
            if dut.m_evt_valid.value and dut.m_evt_ready.value:
                t, i, tk = decode_evt(int(dut.m_evt.value))
                self.events.append((cyc, t, i, tk))
            if dut.stat_evt_drop.value:
                self.drops += 1
            cyc += 1
            dut.m_evt_ready.value = 1 if (self.evt_ready if not callable(self.evt_ready) else self.evt_ready()) else 0

    def tick_at(self, c):
        return self.tick_fn(c) if self.tick_fn else self.tick

    async def step(self, hit=None, host=None):
        """Drive one cycle. hit = (idx, len, fin, rst); host = (we, idx, State or None).
        Returns True if the host op was accepted this cycle."""
        dut = self.dut
        c = self.cycle
        dut.cfg_tick.value = self.tick_at(c)

        if hit is not None:
            idx, length, fin, rst = hit
            dut.s_hit_valid.value = 1
            dut.s_hit_idx.value = idx
            dut.s_hit_len.value = length
            dut.s_hit_fin.value = fin
            dut.s_hit_rst.value = rst
        else:
            dut.s_hit_valid.value = 0

        if host is not None:
            we, idx, st = host
            dut.host_st_valid.value = 1
            dut.host_st_we.value = we
            dut.host_st_idx.value = idx
            dut.host_st_wdata.value = st.pack() if st is not None else 0
        else:
            dut.host_st_valid.value = 0

        await RisingEdge(dut.clk)
        self.cycle += 1

        accepted = False
        if host is not None:
            assert dut.bubble_req.value, "bubble_req low while a host op waits"
            accepted = bool(dut.host_st_ready.value)

        # apply to model in issue order (hit wins the cycle)
        t = self.tick_at(c + self.ram_pipe)
        expected_evt = None
        if hit is not None:
            ev = self.model.hit(hit[0], hit[1], hit[2], hit[3], t)
            if ev is not None:
                expected_evt = (ev, hit[0], self.tick_at(c + self.ram_pipe + 1))
        if accepted:
            we, idx, st = host
            if we:
                self.model.write(idx, st)
            else:
                self.expect_read.append(self.model.read(idx))

        dut.s_hit_valid.value = 0
        dut.host_st_valid.value = 0
        return accepted, expected_evt

    async def idle(self, n):
        for _ in range(n):
            await self.step()

    async def host_op(self, we, idx, st=None):
        while True:
            acc, _ = await self.step(host=(we, idx, st))
            if acc:
                return

    async def host_write(self, idx, st):
        await self.host_op(1, idx, st)

    async def host_read(self, idx):
        n = len(self.rdata)
        await self.host_op(0, idx)
        while len(self.rdata) <= n:
            await self.step()
        return self.rdata[n]

    async def drain(self):
        await self.idle(self.ram_pipe + 8)

    async def clear(self):
        self.dut.clear_start.value = 1
        await self.step()
        self.dut.clear_start.value = 0
        await self.step()
        while self.dut.clear_busy.value:
            await self.step()
        await self.drain()
        self.model.clear()

    def check_reads(self):
        assert len(self.rdata) == len(self.expect_read), (len(self.rdata), len(self.expect_read))
        for k, (got, exp) in enumerate(zip(self.rdata, self.expect_read)):
            assert got == exp, f"read {k}: got {got} expected {exp}"


async def setup(dut):
    tb = TB(dut)
    tb.expect_read = []
    await tb.reset()
    await tb.clear()
    tb.rdata.clear()
    tb.expect_read.clear()
    return tb


async def run_hits_exact(dut):
    tb = await setup(dut)
    tb.tick = 1234

    for gap in range(0, tb.ram_pipe + 3):
        idx = 3 + gap
        await tb.host_write(idx, State(valid=1, tcp=1, ts=5))
        total = 0
        for k in range(40):
            length = random.randrange(60, 1514)
            total += length
            await tb.step(hit=(idx, length, 0, 0))
            await tb.idle(gap)
        await tb.drain()
        s = await tb.host_read(idx)
        tb.log.info("gap %d: %s", gap, s)
        assert s.pkts == 40 and s.bytes == total and s.ts == 1234 and s.valid == 1 and s.tcp == 1

    # two indices alternating and in bursts, every cycle
    a, b = 100, 101
    for idx in (a, b):
        await tb.host_write(idx, State(valid=1))
    seq = [a, b] * 20 + [a] * 7 + [b] * 5 + [a, a, b, b] * 6
    for idx in seq:
        await tb.step(hit=(idx, 64, 0, 0))
    await tb.drain()
    for idx in (a, b):
        s = await tb.host_read(idx)
        assert s.pkts == seq.count(idx) and s.bytes == 64 * seq.count(idx)

    tb.check_reads()


async def run_host_rw(dut):
    tb = await setup(dut)
    written = {}
    for k in range(200):
        idx = random.randrange(tb.n)
        st = State(valid=random.randrange(2), fin=random.randrange(2), rst=random.randrange(2),
                   evp=random.randrange(2), tcp=random.randrange(2), ts=random.getrandbits(32),
                   pkts=random.getrandbits(48), bytes=random.getrandbits(56))
        await tb.host_write(idx, st)
        written[idx] = st
        if random.random() < 0.3:
            await tb.host_read(random.choice(list(written)))
    for idx in written:
        await tb.host_read(idx)
    await tb.drain()
    tb.check_reads()


async def run_events_finrst(dut):
    tb = await setup(dut)
    tb.tick = 77
    cases = [(10, 1, 0, EVT_FIN), (11, 0, 1, EVT_RST), (12, 1, 1, EVT_FIN), (13, 0, 0, None)]
    for idx, fin, rst, _ in cases:
        await tb.host_write(idx, State(valid=1, tcp=1))
    tb.events.clear()
    for idx, fin, rst, _ in cases:
        await tb.step(hit=(idx, 100, fin, rst))
    await tb.drain()
    got = [(t, i, tk) for _, t, i, tk in tb.events]
    exp = [(ev, idx, 77) for idx, fin, rst, ev in cases if ev is not None]
    assert got == exp, (got, exp)
    for idx, fin, rst, _ in cases:
        s = await tb.host_read(idx)
        assert s.fin == fin and s.rst == rst and s.pkts == 1
    tb.check_reads()


async def run_idle_scan(dut):
    tb = await setup(dut)
    dut.cfg_thresh_tcp.value = 100
    dut.cfg_thresh_udp.value = 10
    tb.tick = 1000

    entries = {}
    for idx in random.sample(range(tb.n), tb.n // 2):
        st = State(valid=random.random() < 0.9, tcp=random.randrange(2), evp=random.random() < 0.1,
                   ts=random.choice([1000, 995, 989, 950, 899, 900, 0, 0xfffffff0 if tb.n else 0]))
        entries[idx] = st
        await tb.host_write(idx, st)

    idle = {i for i, s in entries.items() if StateModel.is_idle(s, 1000, 100, 10)}
    tb.log.info("%d entries, %d idle expected", len(entries), len(idle))

    tb.events.clear()
    dut.cfg_scan_interval.value = 1
    dut.cfg_scan_en.value = 1
    await tb.idle(3 * tb.n)
    dut.cfg_scan_en.value = 0
    await tb.drain()

    got = [i for _, t, i, _ in tb.events if t == EVT_IDLE]
    assert sorted(got) == sorted(idle), (sorted(got), sorted(idle))
    for i in idle:
        s = await tb.host_read(i)
        assert s.evp == 1
    tb.expect_read = list(tb.rdata)  # evp written by scanner is outside the op model

    # a hit clears evp; after the tick advances that entry (only) is reported again
    i = min(idle)
    await tb.step(hit=(i, 64, 0, 0))
    await tb.drain()
    tb.tick = 1000 + 200
    tb.events.clear()
    dut.cfg_scan_en.value = 1
    await tb.idle(3 * tb.n)
    dut.cfg_scan_en.value = 0
    await tb.drain()
    got = [i2 for _, t, i2, _ in tb.events if t == EVT_IDLE]
    newly = {j for j, s in entries.items() if j not in idle and StateModel.is_idle(s, 1200, 100, 10)}
    assert sorted(got) == sorted(newly | {i}), (sorted(got), sorted(newly | {i}))


async def run_scan_rate(dut):
    tb = await setup(dut)
    dut.cfg_thresh_udp.value = 0
    tb.tick = 100
    for idx in range(tb.n):
        await tb.host_write(idx, State(valid=1, tcp=0, ts=0))
    for interval in (1, 3, 7):
        # re-arm
        for idx in range(tb.n):
            await tb.host_write(idx, State(valid=1, tcp=0, ts=0))
        tb.events.clear()
        dut.cfg_scan_interval.value = interval
        dut.cfg_scan_en.value = 1
        await tb.idle(tb.n * interval + 50)
        dut.cfg_scan_en.value = 0
        await tb.drain()
        cyc = [c for c, t, _, _ in tb.events if t == EVT_IDLE]
        assert len(cyc) == tb.n, (interval, len(cyc))
        diffs = {b - a for a, b in zip(cyc, cyc[1:])}
        tb.log.info("interval %d: event spacing %s", interval, diffs)
        assert diffs == {interval}, (interval, diffs)


async def run_fifo_full(dut):
    tb = await setup(dut)
    dut.cfg_thresh_udp.value = 0
    tb.tick = 100
    for idx in range(tb.n):
        await tb.host_write(idx, State(valid=1, tcp=0, ts=0))
    fin_idx = 7

    tb.events.clear()
    tb.evt_ready = False
    dut.cfg_scan_interval.value = 1
    dut.cfg_scan_en.value = 1
    await tb.idle(500)
    stored_scan = int(dut.uut.evt_count.value) + int(dut.uut.evt_out_valid_reg.value)
    tb.log.info("scanner paused with %d events queued", stored_scan)
    assert stored_scan < tb.n
    assert int(dut.uut.evt_free.value) < 4 + tb.ram_pipe + 2 + 1

    # FIN hits until well past full
    n_fin = 20
    for k in range(n_fin):
        await tb.step(hit=(fin_idx, 64, 1, 0))
    await tb.drain()
    assert tb.drops > 0
    assert int(dut.uut.evt_full.value)

    tb.evt_ready = True
    await tb.idle(6 * tb.n)
    dut.cfg_scan_en.value = 0
    await tb.drain()

    fins = [e for e in tb.events if e[1] == EVT_FIN]
    ovfs = [e for e in tb.events if e[1] == EVT_OVF]
    idles = [e[2] for e in tb.events if e[1] == EVT_IDLE]
    tb.log.info("fin %d drop %d ovf %d idle %d", len(fins), tb.drops, len(ovfs), len(idles))
    assert len(fins) + tb.drops == n_fin
    assert len(ovfs) == 1
    # the overflow marker follows the last delivered FIN event
    assert tb.events.index(ovfs[0]) > tb.events.index(fins[-1])
    # every idle entry reported exactly once (fin_idx was hit, so its evp may have been cleared)
    assert sorted(set(idles)) == list(range(tb.n))
    assert len(idles) - len(set(idles)) <= 1


async def run_clear(dut):
    tb = await setup(dut)
    for k in range(50):
        await tb.host_write(random.randrange(tb.n), State(valid=1, pkts=5, ts=9))
    await tb.clear()
    for idx in range(tb.n):
        await tb.host_read(idx)
    await tb.drain()
    tb.check_reads()
    assert all(s == State() for s in tb.rdata)


async def run_random_mixed(dut):
    tb = await setup(dut)
    tb.tick_fn = lambda c: 5000 + c // 50
    # scanner runs (ops compete for slots) but thresholds never trigger
    dut.cfg_scan_interval.value = 2
    dut.cfg_scan_en.value = 1

    hot = random.sample(range(tb.n), 6)
    expected_evts = []
    host_pending = None
    for k in range(6000):
        hit = None
        if random.random() < 0.6:
            idx = random.choice(hot) if random.random() < 0.7 else random.randrange(tb.n)
            r = random.random()
            hit = (idx, random.randrange(60, 9000), int(r < 0.03), int(0.03 <= r < 0.06))
        if host_pending is None and random.random() < 0.2:
            idx = random.choice(hot) if random.random() < 0.5 else random.randrange(tb.n)
            if random.random() < 0.5:
                st = State(valid=1, tcp=random.randrange(2), ts=random.getrandbits(32),
                           pkts=random.getrandbits(40), bytes=random.getrandbits(50))
                host_pending = (1, idx, st)
            else:
                host_pending = (0, idx, None)
        acc, ev = await tb.step(hit=hit, host=host_pending)
        if acc:
            host_pending = None
        if ev is not None:
            expected_evts.append(ev)
    while host_pending is not None:
        acc, _ = await tb.step(host=host_pending)
        if acc:
            host_pending = None
    for idx in hot:
        await tb.host_read(idx)
    dut.cfg_scan_en.value = 0
    await tb.drain()

    tb.check_reads()
    got = [(t, i, tk) for _, t, i, tk in tb.events]
    assert got == expected_evts, (got[:10], expected_evts[:10])
    tb.log.info("%d reads, %d events checked", len(tb.rdata), len(got))


for _f in [run_hits_exact, run_host_rw, run_events_finrst, run_idle_scan, run_scan_rate,
           run_fifo_full, run_clear, run_random_mixed]:
    globals()["test_" + _f.__name__[4:]] = cocotb.test()(_f)


# cocotb-test

tests_dir = os.path.dirname(__file__)
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'rtl'))


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


@pytest.mark.parametrize("ram_pipe", [2, 3, 4])
def test_natgw_state(request, ram_pipe):
    dut = "natgw_state"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(rtl_dir, f"{dut}.f"),
        os.path.join(tests_dir, f"{toplevel}.sv"),
    ]

    verilog_sources = process_f_files(verilog_sources)

    parameters = {}

    parameters['IDX_W'] = 8
    parameters['RAM_PIPE'] = ram_pipe
    parameters['EVT_DEPTH'] = 32

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
