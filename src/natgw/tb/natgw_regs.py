# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim register access (host side)

Wraps any pair of async 32-bit read/write callables (an AXI-Lite master in the
shim testbench, a PCIe BAR in the system testbench). Mirrors the register map
in natgw_regs.sv.

"""

from natgw_model import Entry, NextHop, State, to_words, from_words, CuckooTable

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
    def __init__(self, read32, write32, base=0):
        self._rd = read32
        self._wr = write32
        self.base = base

    async def rd(self, reg):
        return await self._rd(self.base + reg)

    async def wr(self, reg, val):
        await self._wr(self.base + reg, val & 0xffffffff)

    async def flush(self):
        """Read back so that earlier posted writes have taken effect (PCIe writes are posted)."""
        await self.rd(REG_ID)

    async def caps(self):
        v = await self.rd(REG_CAPS)
        return {"idx_w": v & 0xff, "lanes": (v >> 8) & 0xff, "punt_hdr_len": (v >> 16) & 0xff}

    async def set_ctrl(self, enable, punt_hdr=False, bypass=0x00, egress_en=0xff):
        await self.wr(REG_CTRL, (1 if enable else 0) | (2 if punt_hdr else 0) | (bypass << 8) | (egress_en << 16))
        await self.flush()

    async def clear(self, poll=None):
        await self.wr(REG_CLEAR, 1)
        await self.wait_clear(poll)

    async def wait_clear(self, poll=None):
        while await self.rd(REG_CLEAR) & 1:
            if poll:
                await poll()

    async def set_seeds(self, seed0, seed1):
        await self.wr(REG_SEED0, seed0)
        await self.wr(REG_SEED1, seed1)

    async def write_entry(self, idx, entry):
        bits = entry.pack() if entry is not None else 0
        for k, w in enumerate(to_words(bits, 7)):
            await self.wr(REG_ENT_DATA + 4*k, w)
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_WR_ENT)

    async def clear_entry(self, idx):
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_CLR)

    async def apply(self, writes):
        """Apply CuckooTable insert/delete writes in order."""
        for idx, entry in writes:
            if entry is None:
                await self.clear_entry(idx)
            else:
                await self.write_entry(idx, entry)
        await self.flush()

    async def read_entry(self, idx):
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_RD_ENT)
        words = [await self.rd(REG_ENT_DATA + 4*k) for k in range(7)]
        return Entry.unpack(from_words(words))

    async def write_state(self, idx, state):
        for k, w in enumerate(to_words(state.pack(), 5)):
            await self.wr(REG_ST_DATA + 4*k, w)
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_WR_ST)

    async def read_state(self, idx):
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_RD_ST)
        words = [await self.rd(REG_ST_DATA + 4*k) for k in range(5)]
        return State.unpack(from_words(words))

    async def write_nh(self, idx, nh):
        for k, w in enumerate(to_words(nh.pack(), 4)):
            await self.wr(REG_NH_DATA + 4*k, w)
        await self.wr(REG_NH_INDEX, idx)
        await self.wr(REG_NH_CMD, 1)
        await self.flush()

    async def read_nh(self, idx):
        await self.wr(REG_NH_INDEX, idx)
        await self.wr(REG_NH_CMD, 2)
        words = [await self.rd(REG_NH_DATA + 4*k) for k in range(4)]
        return NextHop.unpack(from_words(words))

    async def pop_event(self):
        """Return (type, idx, tick) or None."""
        if not (await self.rd(REG_EVT_STATUS) & 1):
            return None
        lo = await self.rd(REG_EVT_LO)
        hi = await self.rd(REG_EVT_HI)
        return (hi >> 28) & 0xf, hi & 0xffffff, lo

    async def read_stat(self, lane, n):
        a = REG_STATS + lane*0x100 + n*8
        lo = await self.rd(a)
        hi = await self.rd(a + 4)
        return (hi << 32) | lo

    # ---------------------------------------------------------------- DDR tier

    async def ddr_status(self):
        v = await self.rd(REG_DDR_STATUS)
        return {"present": bool(v & 1), "calibrated": bool(v & 2), "enabled": bool(v & 4),
                "clearing": bool(v & 8), "active": bool(v & 16), "bucket_w": (v >> 8) & 0xff,
                "max_out": (v >> 16) & 0xff}

    async def ddr_enable(self, enable):
        await self.wr(REG_DDR_CTRL, 1 if enable else 0)
        await self.flush()

    async def ddr_clear(self, enable_after=False, poll=None):
        """zero the DDR table and the activity bitmap (DDR is random after power-up)"""
        await self.wr(REG_DDR_CTRL, 2)
        while (await self.ddr_status())["clearing"]:
            if poll:
                await poll()
        if enable_after:
            await self.ddr_enable(True)

    async def write_ddr_entry(self, idx, entry):
        for k, w in enumerate(to_words(entry.pack(), 7)):
            await self.wr(REG_ENT_DATA + 4*k, w)
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_DDR_WR)

    async def clear_ddr_entry(self, idx):
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_DDR_CLR)

    async def read_ddr_entry(self, idx):
        await self.wr(REG_INDEX, idx)
        await self.wr(REG_CMD, CMD_DDR_RD)
        words = [await self.rd(REG_ENT_DATA + 4*k) for k in range(7)]
        return Entry.unpack(from_words(words))

    async def apply_ddr(self, writes):
        """Apply DdrTable insert/delete writes in order."""
        for idx, entry in writes:
            if entry is None:
                await self.clear_ddr_entry(idx)
            else:
                await self.write_ddr_entry(idx, entry)
        await self.flush()

    async def read_activity(self, word):
        """read and clear 64 activity bits (DDR entries 64*word + n)"""
        await self.wr(REG_INDEX, word)
        await self.wr(REG_CMD, CMD_ACT_RC)
        lo = await self.rd(REG_ACT_LO)
        hi = await self.rd(REG_ACT_HI)
        return lo | (hi << 32)

    async def ddr_stats(self):
        return {"lookups": await self.rd(REG_DDR_LOOKUPS), "hits": await self.rd(REG_DDR_HITS),
                "skips": await self.rd(REG_DDR_SKIPS),
                "read_errors": await self.rd(REG_DDR_RERR)}
