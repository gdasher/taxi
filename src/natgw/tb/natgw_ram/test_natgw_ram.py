#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim: natgw_ram testbench

Random reads on both ports and writes on port B against a reference memory:
a read presented in cycle t returns, PIPE cycles later, the word as written by
every write presented before cycle t (a write takes effect one cycle after it
is presented). Small banks exercise the bank decode and both mux levels.

"""

import os
import random

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge


@cocotb.test()
async def run_test_random(dut):
    data_w = int(dut.DATA_W.value)
    addr_w = int(dut.ADDR_W.value)
    pipe = int(dut.PIPE.value)
    words = 2**addr_w
    rng = random.Random(int(os.environ.get("SEED", "1")))

    cocotb.start_soon(Clock(dut.clk, 4, units="ns").start())
    for s in (dut.a_en, dut.b_en, dut.b_we):
        s.value = 0
    dut.a_addr.value = 0
    dut.b_addr.value = 0
    dut.b_din.value = 0

    # initialise every word through port B
    mem = [0] * words
    for addr in range(words):
        await FallingEdge(dut.clk)
        mem[addr] = rng.getrandbits(data_w)
        dut.b_en.value = 1
        dut.b_we.value = 1
        dut.b_addr.value = addr
        dut.b_din.value = mem[addr]
    await FallingEdge(dut.clk)
    dut.b_en.value = 0
    for _ in range(pipe + 2):
        await RisingEdge(dut.clk)

    expect = {}        # sampling edge -> [(port, value)]
    edge = 0
    checked = {"a": 0, "b": 0}
    hot = [rng.randrange(words) for _ in range(4)]   # back-to-back same-address traffic

    def pick():
        return rng.choice(hot) if rng.random() < 0.3 else rng.randrange(words)

    for cycle in range(6000):
        await FallingEdge(dut.clk)
        a_en = rng.random() < 0.7
        b_en = rng.random() < 0.7
        b_we = rng.random() < 0.6
        a_addr, b_addr = pick(), pick()
        din = rng.getrandbits(data_w)
        dut.a_en.value = int(a_en)
        dut.a_addr.value = a_addr
        dut.b_en.value = int(b_en)
        dut.b_we.value = int(b_we)
        dut.b_addr.value = b_addr
        dut.b_din.value = din

        # reads see the memory before this cycle's write
        res = []
        if a_en:
            res.append(("a", mem[a_addr]))
        if b_en and not b_we:
            res.append(("b", mem[b_addr]))
        if b_en and b_we:
            mem[b_addr] = din
        # sampled at the next rising edge; visible PIPE-1 edges after it
        expect[edge + 1 + pipe - 1] = res

        await RisingEdge(dut.clk)
        edge += 1
        await ReadOnly()
        for port, val in expect.pop(edge, []):
            got = int((dut.a_dout if port == "a" else dut.b_dout).value)
            assert got == val, f"cycle {cycle}: port {port} read {got:#x}, expected {val:#x}"
            checked[port] += 1

    dut._log.info("checked %d port A and %d port B reads", checked["a"], checked["b"])
    assert checked["a"] > 1000 and checked["b"] > 500


# cocotb-test

tests_dir = os.path.dirname(__file__)
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'rtl'))


# (ADDR_W, BANK_AW, PIPE): 1 bank; 8 banks (one mux level); 32 banks as in the
# 256k build (four of eight first-level groups); 64 banks; spare stages used
# as bank and output registers
@pytest.mark.parametrize("addr_w,bank_aw,pipe", [
    (5, 13, 2), (5, 13, 4),
    (5, 2, 3), (5, 2, 4),
    (7, 2, 4), (7, 2, 5), (7, 2, 6),
    (8, 2, 4), (8, 2, 5),
])
def test_natgw_ram(request, addr_w, bank_aw, pipe):
    dut = "natgw_ram"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(rtl_dir, f"{dut}.sv"),
        os.path.join(tests_dir, f"{toplevel}.sv"),
    ]

    parameters = {'DATA_W': 20, 'ADDR_W': addr_w, 'PIPE': pipe, 'BANK_AW': bank_aw}
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
