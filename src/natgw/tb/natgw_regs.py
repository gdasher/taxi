# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim register access (host side), through libnatgw

Wraps any pair of async 32-bit read/write callables (an AXI-Lite master in the
shim testbench, a PCIe BAR in the system testbench) and drives them with the
host library's own code. The register map constants below mirror
natgw_regs.sv for tests that poke registers directly.

"""

import ctypes as C

from cocotb.triggers import Lock

try:
    from cocotb import bridge, resume
except ImportError:          # cocotb 2.0 keeps them in _bridge
    from cocotb._bridge import bridge, resume

import natgw_clib as clib
from natgw_model import (Entry, NextHop, State, to_words, from_words, CuckooTable,  # noqa: F401
                         _entry_c, _entry_py, _nh_c)

REG_ID = 0x0000
REG_VERSION = 0x0004
REG_CAPS = 0x0008
REG_SCRATCH = 0x000C
REG_CTRL = 0x0010
REG_CLEAR = 0x0014
REG_SEED0 = 0x0020
REG_SEED1 = 0x0024
REG_TICK_DIV = 0x0028
REG_TICK = 0x002C
REG_THRESH_TCP = 0x0030
REG_THRESH_UDP = 0x0034
REG_SCAN = 0x0038
REG_BUBBLE = 0x003C
REG_DDR_STATUS = 0x0060
REG_DDR_CTRL = 0x0064
REG_DDR_LOOKUPS = 0x0068
REG_DDR_HITS = 0x006C
REG_DDR_SKIPS = 0x0070
REG_DDR_RERR = 0x0074
REG_ACT_LO = 0x0148
REG_ACT_HI = 0x014C
REG_ENT_DATA = 0x0100
REG_ST_DATA = 0x0120
REG_INDEX = 0x0140
REG_CMD = 0x0144
REG_NH_INDEX = 0x0200
REG_NH_CMD = 0x0204
REG_NH_DATA = 0x0210
REG_EVT_STATUS = 0x0300
REG_EVT_LO = 0x0304
REG_EVT_HI = 0x0308
REG_EVT_DROPS = 0x0310
REG_STATS = 0x1000

CMD_WR_ENT = 1
CMD_WR_ST = 2
CMD_RD_ENT = 3
CMD_RD_ST = 4
CMD_CLR = 5
CMD_DDR_WR = 6
CMD_DDR_CLR = 7
CMD_DDR_RD = 8
CMD_ACT_RC = 9

NAT_ID = 0x4E415447

STAT_DROP = 16
STAT_BAD = 17


class NatRegs:
    """The shim's registers driven by libnatgw, the host library the real
    driver uses: every operation below is a libnatgw call whose register
    reads and writes become bus transactions on the simulated design
    (read32/write32 coroutines, e.g. an AXI-Lite master or the driver's BAR).
    The C code runs in a cocotb bridge thread and blocks on each access, so
    the register protocol under test is exactly the host's. rd()/wr() remain
    for direct register pokes."""

    def __init__(self, read32, write32, base=0):
        self._rd = read32
        self._wr = write32
        self.base = base
        self._dev = None
        self._lock = None

        resume_rd = resume(self._raw_rd)
        resume_wr = resume(self._raw_wr)

        def c_rd(ctx, off):
            return resume_rd(off)

        def c_wr(ctx, off, val):
            resume_wr(off, val)

        # keep the callback objects alive as long as this object
        self._c_rd = clib.RD(c_rd)
        self._c_wr = clib.WR(c_wr)
        self._io = clib.Io(self._c_rd, self._c_wr, None)

    async def _raw_rd(self, off):
        return (await self._rd(self.base + off)) & 0xffffffff

    async def _raw_wr(self, off, val):
        await self._wr(self.base + off, val & 0xffffffff)

    async def _call(self, fn, *args):
        """run fn(dev, *args) (blocking libnatgw code) in a bridge thread"""
        if self._lock is None:
            self._lock = Lock()
        async with self._lock:
            if self._dev is None:
                self._dev = clib.Dev()

                def init():
                    return clib.lib.natgw_dev_init(C.byref(self._dev), C.byref(self._io))
                rc = await bridge(init)()
                assert rc == 0, f"natgw_dev_init: {rc}"

            def call():
                return fn(C.byref(self._dev), *args)
            return await bridge(call)()

    async def rd(self, reg):
        return await self._rd(self.base + reg)

    async def wr(self, reg, val):
        await self._wr(self.base + reg, val & 0xffffffff)

    async def flush(self):
        """read back so that earlier posted writes have taken effect"""
        await self._call(clib.lib.natgw_dev_flush)

    async def caps(self):
        await self._call(clib.lib.natgw_dev_flush)
        d = self._dev
        return {"idx_w": d.idx_w, "lanes": d.lanes, "punt_hdr_len": d.punt_hdr_len}

    async def set_ctrl(self, enable, punt_hdr=False, bypass=0x00, egress_en=0xff):
        await self._call(clib.lib.natgw_dev_set_ctrl, bool(enable), bool(punt_hdr), bypass & 0xff, egress_en & 0xff)

    async def clear(self, poll=None):
        assert await self._call(clib.lib.natgw_dev_clear, 1000000) == 0, "clear timed out"

    async def wait_clear(self, poll=None):
        """wait for a clear started elsewhere (e.g. by a reset)"""
        while await self.rd(REG_CLEAR) & 1:
            if poll:
                await poll()

    async def set_seeds(self, seed0, seed1):
        await self._call(clib.lib.natgw_dev_set_seeds, seed0 & 0xffffffff, seed1 & 0xffffffff)

    async def write_entry(self, idx, entry):
        if entry is None:
            await self.clear_entry(idx)
        else:
            await self._call(clib.lib.natgw_dev_write_entry, idx, C.byref(_entry_c(entry)))

    async def clear_entry(self, idx):
        await self._call(clib.lib.natgw_dev_clear_entry, idx)

    @staticmethod
    def _ops(writes):
        ops = (clib.Write * max(len(writes), 1))()
        for k, (i, e) in enumerate(writes):
            ops[k].idx = i
            ops[k].clear = e is None
            if e is not None:
                ops[k].entry = _entry_c(e)
        return ops

    async def apply(self, writes):
        """apply table insert/delete writes in order (natgw_dev_apply)"""
        writes = list(writes)
        await self._call(clib.lib.natgw_dev_apply, self._ops(writes), len(writes))

    async def read_entry(self, idx):
        e = clib.Entry()
        await self._call(clib.lib.natgw_dev_read_entry, idx, C.byref(e))
        return _entry_py(e)

    async def write_state(self, idx, state):
        # no host need to write state: a direct register poke for tests
        for k, w in enumerate(to_words(state.pack(), 5)):
            await self.wr(REG_ST_DATA + 4*k, w)
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_WR_ST)

    async def read_state(self, idx):
        s = clib.State()
        await self._call(clib.lib.natgw_dev_read_state, idx, C.byref(s))
        return State(valid=s.valid, fin=s.fin, rst=s.rst, evp=s.evp, tcp=s.tcp, ts=s.ts, pkts=s.pkts,
                     bytes=s.bytes)

    async def write_nh(self, idx, nh):
        await self._call(clib.lib.natgw_dev_write_nh, idx, C.byref(_nh_c(nh)))

    async def read_nh(self, idx):
        n = clib.NextHop()
        await self._call(clib.lib.natgw_dev_read_nh, idx, C.byref(n))
        return NextHop(dst_mac=int.from_bytes(bytes(n.dst_mac), "big"),
                       src_mac=int.from_bytes(bytes(n.src_mac), "big"), lane=n.lane, vlan=n.vlan, vid=n.vid,
                       valid=n.valid)

    async def pop_event(self):
        """Return (type, idx, tick) or None."""
        ev = clib.Event()
        if not await self._call(clib.lib.natgw_dev_pop_event, C.byref(ev)):
            return None
        return ev.type, ev.idx, ev.tick

    async def read_stat(self, lane, n):
        return await self._call(clib.lib.natgw_dev_read_stat, lane, n)

    # ---------------------------------------------------------------- DDR tier

    async def ddr_status(self):
        st = clib.DdrStatus()
        await self._call(clib.lib.natgw_dev_ddr_status, C.byref(st))
        return {"present": st.present, "calibrated": st.calibrated, "enabled": st.enabled,
                "clearing": st.clearing, "active": st.active, "bucket_w": st.bucket_w, "max_out": st.max_out}

    async def ddr_enable(self, enable):
        await self._call(clib.lib.natgw_dev_ddr_enable, bool(enable))

    async def ddr_clear(self, enable_after=False, poll=None):
        """zero the DDR table and the activity bitmap (DDR is random after power-up)"""
        assert await self._call(clib.lib.natgw_dev_ddr_clear, 10000000) == 0, "DDR clear timed out"
        if enable_after:
            await self.ddr_enable(True)

    async def write_ddr_entry(self, idx, entry):
        await self._call(clib.lib.natgw_dev_write_ddr_entry, idx, C.byref(_entry_c(entry)))

    async def clear_ddr_entry(self, idx):
        await self._call(clib.lib.natgw_dev_clear_ddr_entry, idx)

    async def read_ddr_entry(self, idx):
        e = clib.Entry()
        await self._call(clib.lib.natgw_dev_read_ddr_entry, idx, C.byref(e))
        return _entry_py(e)

    async def apply_ddr(self, writes):
        """apply DDR table insert/delete writes in order (natgw_dev_apply_ddr)"""
        writes = list(writes)
        await self._call(clib.lib.natgw_dev_apply_ddr, self._ops(writes), len(writes))

    async def read_activity(self, word):
        """read and clear 64 activity bits (DDR entries 64*word + n)"""
        return await self._call(clib.lib.natgw_dev_read_activity, word)

    async def ddr_stats(self):
        lk, ht, sk = C.c_uint32(), C.c_uint32(), C.c_uint32()
        await self._call(clib.lib.natgw_dev_ddr_stats, C.byref(lk), C.byref(ht), C.byref(sk))
        rerr = await self._call(clib.lib.natgw_dev_ddr_read_errors)
        return {"lookups": lk.value, "hits": ht.value, "skips": sk.value, "read_errors": rerr}
