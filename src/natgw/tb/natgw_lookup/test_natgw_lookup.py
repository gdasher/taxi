#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway lookup engine testbench

"""

import logging
import os
import random
import sys

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

try:
    import natgw_model as nm
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    try:
        import natgw_model as nm
    finally:
        del sys.path[0]

LANES = 8
KEY_W = nm.KEY_W

# result_t, LSB first
RES_FIELDS = [("hit", 1), ("idx", 32), ("xlate_dst", 1), ("new_ip", 32), ("new_port", 16),
              ("dec_ttl", 1), ("nh_idx", 10), ("nh", 128), ("hash", 32)]


def unpack_res(v):
    return nm._unpack(RES_FIELDS, v)


def rand_key(lane=None):
    return nm.Key(lane=random.randrange(8) if lane is None else lane, vid=random.randrange(4096) if random.random() < 0.3 else 0,
                  tcp=random.randrange(2), sip=random.getrandbits(32), dip=random.getrandbits(32),
                  sport=random.getrandbits(16), dport=random.getrandbits(16))


def rand_entry(key, nh_count=16):
    return nm.Entry(key=key, xlate_dst=random.randrange(2), new_ip=random.getrandbits(32),
                    new_port=random.getrandbits(16), dec_ttl=random.randrange(2), nh_idx=random.randrange(nh_count))


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.DEBUG)

        self.bucket_w = int(os.environ.get("PARAM_BUCKET_W", "6"))
        self.ram_pipe = int(os.environ.get("PARAM_RAM_PIPE", "3"))

        cocotb.start_soon(Clock(dut.clk, 4, unit="ns").start())

        self.lane_q = [[] for _ in range(LANES)]
        self.ent_q = []
        self.nh_q = []
        self.ent_rd = []
        self.nh_rd = []
        self.results = []      # (cycle, lane, res dict, hit tuple or None)
        self.accepts = []      # (cycle, lane, item)
        self.cycle = 0
        self.lane_enable = [True]*LANES

        for name in ["s_key_valid", "s_key", "s_key_lookup", "s_key_fin", "s_key_rst", "s_key_len",
                     "bubble_req", "cfg_bubble_period", "host_ent_valid", "host_ent_we", "host_ent_idx",
                     "host_ent_wdata", "host_nh_valid", "host_nh_we", "host_nh_idx", "host_nh_wdata", "clear_start"]:
            getattr(dut, name).setimmediatevalue(0)
        dut.cfg_seed0.setimmediatevalue(0xffffffff)
        dut.cfg_seed1.setimmediatevalue(0xffffffff)

        self.model = nm.CuckooTable(self.bucket_w)
        self.nh = {}

    def set_seeds(self, s0, s1):
        self.dut.cfg_seed0.value = s0
        self.dut.cfg_seed1.value = s1
        self.model = nm.CuckooTable(self.bucket_w, s0, s1)

    async def reset(self):
        self.dut.rst.setimmediatevalue(0)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 1
        for _ in range(3):
            await RisingEdge(self.dut.clk)
        self.dut.rst.value = 0
        await RisingEdge(self.dut.clk)
        # memories survive between tests: clear the tables and next hops
        self.dut.clear_start.value = 1
        await RisingEdge(self.dut.clk)
        self.dut.clear_start.value = 0
        await RisingEdge(self.dut.clk)
        while int(self.dut.clear_busy.value):
            await RisingEdge(self.dut.clk)
        cocotb.start_soon(self._run())
        for i in range(1024):
            self.nh_q.append(("w", i, 0))

    async def _run(self):
        dut = self.dut
        cur = [None]*LANES
        cur_ent = None
        cur_nh = None
        while True:
            await RisingEdge(dut.clk)
            self.cycle += 1

            # drive
            valid = 0
            keys = 0
            lookup = 0
            fin = 0
            rstf = 0
            lens = 0
            for n in range(LANES):
                if cur[n] is None and self.lane_q[n] and self.lane_enable[n]:
                    cur[n] = self.lane_q[n].pop(0)
                it = cur[n]
                if it is not None:
                    valid |= 1 << n
                    keys |= it["key"].pack() << (n*KEY_W)
                    lookup |= it["lookup"] << n
                    fin |= it["fin"] << n
                    rstf |= it["rst"] << n
                    lens |= it["len"] << (n*16)
            dut.s_key_valid.value = valid
            dut.s_key.value = keys
            dut.s_key_lookup.value = lookup
            dut.s_key_fin.value = fin
            dut.s_key_rst.value = rstf
            dut.s_key_len.value = lens

            if cur_ent is None and self.ent_q:
                cur_ent = self.ent_q.pop(0)
            if cur_ent is not None:
                dut.host_ent_valid.value = 1
                dut.host_ent_we.value = cur_ent[0] == "w"
                dut.host_ent_idx.value = cur_ent[1]
                dut.host_ent_wdata.value = cur_ent[2] if cur_ent[0] == "w" else 0
            else:
                dut.host_ent_valid.value = 0

            if cur_nh is None and self.nh_q:
                cur_nh = self.nh_q.pop(0)
            if cur_nh is not None:
                dut.host_nh_valid.value = 1
                dut.host_nh_we.value = cur_nh[0] == "w"
                dut.host_nh_idx.value = cur_nh[1]
                dut.host_nh_wdata.value = cur_nh[2] if cur_nh[0] == "w" else 0
            else:
                dut.host_nh_valid.value = 0

            await ReadOnly()

            # sample handshakes
            ready = int(dut.s_key_ready.value)
            for n in range(LANES):
                if cur[n] is not None and (ready >> n) & 1:
                    self.accepts.append((self.cycle, n, cur[n]))
                    cur[n] = None
            if cur_ent is not None and int(dut.host_ent_ready.value):
                if cur_ent[3] is not None:
                    cur_ent[3](self.cycle)
                cur_ent = None
            if cur_nh is not None and int(dut.host_nh_ready.value):
                cur_nh = None

            # sample outputs
            if int(dut.m_res_valid.value):
                res = unpack_res(int(dut.m_res.value))
                # sideband for a DDR tier
                res["_key"] = int(dut.m_res_key.value)
                res["_h1"] = int(dut.m_res_h1.value)
                res["_lookup"] = int(dut.m_res_lookup.value)
                hit = None
                if int(dut.m_hit_valid.value):
                    hit = (int(dut.m_hit_idx.value), int(dut.m_hit_len.value),
                           int(dut.m_hit_fin.value), int(dut.m_hit_rst.value))
                self.results.append((self.cycle, int(dut.m_res_lane.value), res, hit))
            else:
                assert not int(dut.m_hit_valid.value)
            if int(dut.host_ent_rvalid.value):
                self.ent_rd.append(int(dut.host_ent_rdata.value))
            if int(dut.host_nh_rvalid.value):
                self.nh_rd.append(int(dut.host_nh_rdata.value))

    async def cycles(self, n):
        for _ in range(n):
            await RisingEdge(self.dut.clk)

    async def idle(self):
        while any(self.lane_q) or self.ent_q or self.nh_q:
            await RisingEdge(self.dut.clk)
        await self.cycles(40)

    # host operations
    def write_entry(self, idx, entry, cb=None):
        self.ent_q.append(("w", idx, entry.pack() if entry is not None else 0, cb))

    def read_entry(self, idx):
        self.ent_q.append(("r", idx, 0, None))

    def write_nh(self, idx, nh):
        self.nh[idx] = nh
        self.nh_q.append(("w", idx, nh.pack()))

    def read_nh(self, idx):
        self.nh_q.append(("r", idx, 0))

    def insert(self, entry):
        for idx, e in self.model.insert(entry):
            self.write_entry(idx, e)

    def send(self, key, lane=None, lookup=1, fin=0, rst=0, length=None, tag=None):
        lane = key.lane if lane is None else lane
        it = dict(key=key, lookup=lookup, fin=fin, rst=rst,
                  len=random.randrange(60, 9000) if length is None else length, tag=tag)
        self.lane_q[lane].append(it)
        return it

    def check(self, expect_fn=None):
        """Match results to accepts (per lane, in order) and check against the model."""
        per_lane = [[a for a in self.accepts if a[1] == n] for n in range(LANES)]
        res_lane = [[r for r in self.results if r[1] == n] for n in range(LANES)]
        lat = set()
        for n in range(LANES):
            assert len(per_lane[n]) == len(res_lane[n]), f"lane {n}: {len(per_lane[n])} accepted, {len(res_lane[n])} results"
            for (ac, _, it), (rc, _, res, hit) in zip(per_lane[n], res_lane[n]):
                lat.add(rc - ac)
                if expect_fn is not None:
                    expect_fn(it, res, hit)
                else:
                    self.expect_model(it, res, hit)
        assert len(lat) <= 1, f"latency not fixed: {lat}"
        self.accepts.clear()
        self.results.clear()
        return lat.pop() if lat else None

    def expect_model(self, it, res, hit):
        key = it["key"]
        # the sideband a DDR tier uses: the key, h1 and whether it was looked up
        assert res["_lookup"] == it["lookup"]
        assert res["_key"] == key.pack(), "sideband key"
        if it["lookup"]:
            assert res["_h1"] == self.model.hashes(key)[1], "sideband h1"
        if not it["lookup"]:
            assert res["hit"] == 0 and res["hash"] == 0 and hit is None
            return
        h0 = self.model.hashes(key)[0]
        assert res["hash"] == h0, f"hash {res['hash']:08x} != {h0:08x}"
        idx, e = self.model.lookup(key)
        if e is None:
            assert res["hit"] == 0 and hit is None, f"unexpected hit {res}"
            return
        assert res["hit"] == 1, f"miss for present key {key}"
        assert res["idx"] == idx, f"idx {res['idx']} != {idx}"
        self.expect_action(res, e)
        assert hit == (idx, it["len"], it["fin"], it["rst"]), f"hit stream {hit}"

    def expect_action(self, res, e):
        assert res["xlate_dst"] == e.xlate_dst
        assert res["new_ip"] == e.new_ip
        assert res["new_port"] == e.new_port
        assert res["dec_ttl"] == e.dec_ttl
        assert res["nh_idx"] == e.nh_idx
        nh = self.nh.get(e.nh_idx)
        assert res["nh"] == (nh.pack() if nh is not None else 0), f"nh {res['nh']:x}"


def fill(tb, load, lanes=None):
    target = int(tb.model.size * load)
    keys = []
    while len(tb.model.slots) < target:
        k = rand_key(random.choice(lanes) if lanes else None)
        if k in tb.model.where:
            continue
        try:
            tb.insert(rand_entry(k))
        except nm.TableFull:
            break
        keys.append(k)
    return keys


async def setup_nh(tb, count=16):
    for i in range(count):
        tb.write_nh(i, nm.NextHop(dst_mac=random.getrandbits(48), src_mac=random.getrandbits(48),
                                  lane=random.randrange(8), vlan=random.randrange(2), vid=random.randrange(4096),
                                  valid=int(random.random() < 0.9)))


@cocotb.test()
async def run_test_fill_lookup(dut):
    """Fill to ~90% load, look up present and absent keys on all lanes."""
    tb = TB(dut)
    random.seed(1)
    await tb.reset()
    await setup_nh(tb)
    keys = fill(tb, 0.9)
    await tb.idle()
    tb.log.info("table load %.3f, %d keys", tb.model.load(), len(keys))

    for k in random.sample(keys, min(len(keys), 600)):
        tb.send(k, fin=random.randrange(2), rst=random.randrange(2))
    for _ in range(300):
        tb.send(rand_key())
    for k in random.sample(keys, 100):
        tb.send(k, lookup=0)
    await tb.idle()
    lat = tb.check()
    tb.log.info("latency %d cycles", lat)
    assert lat == 8 + tb.ram_pipe


@cocotb.test()
async def run_test_collisions(dut):
    """Many keys sharing one T0 bucket: overflow into T1 and relocation."""
    tb = TB(dut)
    random.seed(2)
    await tb.reset()
    await setup_nh(tb)
    target = 5
    keys = []
    while len(keys) < 12:
        k = rand_key()
        if tb.model.bucket_of(k, 0) == target and k not in keys:
            keys.append(k)
    inserted = []
    for k in keys:
        try:
            tb.insert(rand_entry(k))
            inserted.append(k)
        except nm.TableFull:
            pass
    tb.log.info("inserted %d of %d colliding keys", len(inserted), len(keys))
    assert len(inserted) >= 8
    fill(tb, 0.5)
    await tb.idle()
    for k in keys*3:
        tb.send(k)
    await tb.idle()
    tb.check()


@cocotb.test()
async def run_test_concurrent_writes(dut):
    """Relocations and updates during continuous lookups: present keys never miss."""
    tb = TB(dut)
    random.seed(3)
    await tb.reset()
    await setup_nh(tb)
    keys = fill(tb, 0.6)
    await tb.idle()

    # stable keys keep their action; probe them continuously while inserting more (forcing relocations)
    probe = random.sample(keys, 64)
    actions = {k: tb.model.slots[tb.model.where[k]] for k in probe}

    def expect(it, res, hit):
        k = it["key"]
        if k in actions:
            assert res["hit"] == 1, f"present key missed during relocation: {k}"
            e = actions[k]
            assert (res["xlate_dst"], res["new_ip"], res["new_port"], res["dec_ttl"], res["nh_idx"]) == \
                (e.xlate_dst, e.new_ip, e.new_port, e.dec_ttl, e.nh_idx)
            assert hit is not None and hit[0] == res["idx"]

    moved = 0
    for rnd in range(40):
        for k in probe:
            tb.send(k)
        before = dict(tb.model.where)
        fill(tb, min(0.6 + 0.008*rnd, 0.93))
        moved += sum(1 for k in probe if tb.model.where.get(k) != before.get(k))
        await tb.cycles(20)
    await tb.idle()
    tb.log.info("probe keys relocated %d times", moved)
    assert moved > 0
    tb.check(expect)

    # final state consistent with the model
    for k in keys:
        tb.send(k)
    await tb.idle()
    tb.check()


@cocotb.test()
async def run_test_delete_update(dut):
    """Deletes and in-place updates."""
    tb = TB(dut)
    random.seed(4)
    await tb.reset()
    await setup_nh(tb)
    keys = fill(tb, 0.7)
    await tb.idle()
    dele = random.sample(keys, 50)
    for k in dele:
        for idx, _ in tb.model.delete(k):
            tb.write_entry(idx, None)
    upd = random.sample([k for k in keys if k not in dele], 50)
    for k in upd:
        tb.insert(rand_entry(k))
    await tb.idle()
    for k in dele + upd:
        tb.send(k)
    await tb.idle()
    tb.check()


@cocotb.test()
async def run_test_host_read_clear(dut):
    """Entry and next-hop reads; table clear."""
    tb = TB(dut)
    random.seed(5)
    await tb.reset()
    await setup_nh(tb, 32)
    keys = fill(tb, 0.5)
    await tb.idle()

    idxs = random.sample(sorted(tb.model.slots), 50) + [i for i in range(tb.model.size) if i not in tb.model.slots][:20]
    for i in idxs:
        tb.read_entry(i)
    for i in range(32):
        tb.read_nh(i)
    await tb.idle()
    assert len(tb.ent_rd) == len(idxs)
    for i, v in zip(idxs, tb.ent_rd):
        e = tb.model.slots.get(i)
        assert v == (e.pack() if e is not None else 0), f"entry read {i}"
    assert tb.nh_rd == [tb.nh[i].pack() for i in range(32)]

    dut.clear_start.value = 1
    await RisingEdge(dut.clk)
    dut.clear_start.value = 0
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.clear_busy.value)
    n = 0
    while int(dut.clear_busy.value):
        await RisingEdge(dut.clk)
        await ReadOnly()
        n += 1
    tb.log.info("clear took %d cycles", n)
    assert n <= max(1 << tb.bucket_w, 1024) + 2
    await RisingEdge(dut.clk)
    tb.model = nm.CuckooTable(tb.bucket_w)
    for k in random.sample(keys, 100):
        tb.send(k)
    tb.ent_rd.clear()
    for i in idxs[:20]:
        tb.read_entry(i)
    await tb.idle()
    tb.check()
    assert tb.ent_rd == [0]*20
    tb.nh_rd.clear()
    for i in range(32):
        tb.read_nh(i)
    await tb.idle()
    assert tb.nh_rd == [0]*32, "next hops not cleared"


@cocotb.test()
async def run_test_throughput(dut):
    """All 8 lanes driving keys every cycle: one lookup per clock, round-robin fairness; bubbles."""
    tb = TB(dut)
    random.seed(6)
    await tb.reset()
    await setup_nh(tb)
    keys = fill(tb, 0.8)
    await tb.idle()

    per = 400
    for n in range(LANES):
        for _ in range(per):
            tb.send(random.choice(keys), lane=n)
    await tb.cycles(50)
    a0 = len(tb.accepts)
    c0 = tb.cycle
    await tb.cycles(1000)
    rate = (len(tb.accepts) - a0) / (tb.cycle - c0)
    tb.log.info("accept rate %.3f per cycle", rate)
    assert rate == 1.0
    await tb.idle()
    counts = [sum(1 for a in tb.accepts if a[1] == n) for n in range(LANES)]
    assert counts == [per]*LANES
    tb.check()

    # bubble insertion: 1 idle cycle every 4 under load
    dut.bubble_req.value = 1
    dut.cfg_bubble_period.value = 4
    for n in range(LANES):
        for _ in range(per):
            tb.send(random.choice(keys), lane=n)
    await tb.cycles(50)
    a0 = len(tb.accepts)
    c0 = tb.cycle
    await tb.cycles(1000)
    rate = (len(tb.accepts) - a0) / (tb.cycle - c0)
    tb.log.info("accept rate with bubbles %.3f per cycle", rate)
    assert abs(rate - 0.75) < 0.01
    await tb.idle()
    dut.bubble_req.value = 0
    tb.check()


@cocotb.test()
async def run_test_seeds(dut):
    """Non-default seeds."""
    tb = TB(dut)
    random.seed(7)
    tb.set_seeds(0x12345678, 0x9abcdef0)
    await tb.reset()
    await setup_nh(tb)
    keys = fill(tb, 0.85)
    await tb.idle()
    for k in keys:
        tb.send(k)
    for _ in range(200):
        tb.send(rand_key())
    await tb.idle()
    tb.check()


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


@pytest.mark.parametrize(("bucket_w", "ram_pipe"), [(6, 3), (6, 2), (12, 4)])
def test_natgw_lookup(request, bucket_w, ram_pipe):
    dut = "natgw_lookup"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(rtl_dir, "natgw_pkg.sv"),
        os.path.join(tests_dir, f"{toplevel}.sv"),
        os.path.join(rtl_dir, f"{dut}.f"),
    ]

    verilog_sources = process_f_files(verilog_sources)

    parameters = {}

    parameters['BUCKET_W'] = bucket_w
    parameters['RAM_PIPE'] = ram_pipe

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
