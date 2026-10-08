#!/usr/bin/env python
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""

NAT gateway shim testbench (V2): all eight lanes at their own clock rates,
AXI-Lite control, natgw_model.ShimModel as the scoreboard.

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
from cocotb.triggers import RisingEdge, Timer, with_timeout

from cocotbext.axi import AxiLiteBus, AxiLiteMaster
from cocotbext.axi import AxiStreamBus, AxiStreamFrame, AxiStreamSource, AxiStreamSink

from scapy.layers.l2 import Ether, Dot1Q, ARP
from scapy.layers.inet import IP, TCP, UDP, ICMP
from scapy.packet import Raw

try:
    import natgw_model as nm
    from natgw_regs import NatRegs, STAT_DROP, STAT_BAD
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    try:
        import natgw_model as nm
        from natgw_regs import NatRegs, STAT_DROP, STAT_BAD
    finally:
        del sys.path[0]

LANES = 8
GW_MAC = 0x02000000aa00
LAN_HOST_MAC = 0x020000001100
WAN_GW_MAC = [0x020000002200, 0x020000003300]
LAN_LANE = 0
WAN_LANES = [1, 2]
PUB_IP = [0xc6336401, 0xcb007101]    # 198.51.100.1, 203.0.113.1
NH_LAN = 1
NH_WAN = [2, 3]


def ip_str(v):
    return str(ipaddress.IPv4Address(v))


def mac_str(v):
    return ':'.join(f'{b:02x}' for b in v.to_bytes(6, 'big'))


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.INFO)

        self.bucket_w = int(dut.BUCKET_W.value)

        cocotb.start_soon(Clock(dut.clk, 4000, units="ps").start())
        for n in range(LANES):
            # lane clocks near 390.625 MHz, each slightly different
            per = 2560 + 2 * ((n % 3) - 1)
            cocotb.start_soon(Clock(getattr(dut, f"mac_rx_clk_{n}"), per, units="ps").start())
            cocotb.start_soon(Clock(getattr(dut, f"mac_tx_clk_{n}"), 2560 - 2 * (n % 2), units="ps").start())

        self.axil = AxiLiteMaster(AxiLiteBus.from_entity(dut.s_axil), dut.clk, dut.rst)
        self.regs = NatRegs(self.axil.read_dword, self.axil.write_dword)

        self.mac_rx = []
        self.mac_tx = []
        self.mac_cpl = []
        self.core_tx = []
        self.core_cpl = []
        self.core_rx = []
        for n in range(LANES):
            rclk = getattr(dut, f"mac_rx_clk_{n}")
            rrst = getattr(dut, f"mac_rx_rst_{n}")
            tclk = getattr(dut, f"mac_tx_clk_{n}")
            trst = getattr(dut, f"mac_tx_rst_{n}")
            self.mac_rx.append(AxiStreamSource(AxiStreamBus.from_entity(dut.mac_rx[n]), rclk, rrst))
            self.core_rx.append(AxiStreamSink(AxiStreamBus.from_entity(dut.core_rx[n]), rclk, rrst))
            self.mac_tx.append(AxiStreamSink(AxiStreamBus.from_entity(dut.mac_tx[n]), tclk, trst))
            self.mac_cpl.append(AxiStreamSource(AxiStreamBus.from_entity(dut.mac_tx_cpl[n]), tclk, trst))
            self.core_tx.append(AxiStreamSource(AxiStreamBus.from_entity(dut.core_tx[n]), tclk, trst))
            self.core_cpl.append(AxiStreamSink(AxiStreamBus.from_entity(dut.core_tx_cpl[n]), tclk, trst))

        self.model = nm.ShimModel(bucket_w=self.bucket_w)
        self.seq = 0

        # received traffic
        self.fwd_rx = [[] for _ in range(LANES)]     # (bytes) shim frames seen on MAC TX, tid 1
        self.host_rx = [[] for _ in range(LANES)]    # host frames seen on MAC TX, tid 0
        self.cpl_ts = [0] * LANES
        for n in range(LANES):
            cocotb.start_soon(self._mac_tx_loop(n))

    async def _mac_tx_loop(self, n):
        """Emulate the MAC: collect frames and return a completion carrying tid."""
        while True:
            frame = await self.mac_tx[n].recv()
            tid = frame.tid[0] if isinstance(frame.tid, list) else frame.tid
            data = bytes(frame.tdata)
            if tid == 1:
                self.fwd_rx[n].append(data)
            elif tid == 0:
                self.host_rx[n].append(data)
            else:
                raise AssertionError(f"unexpected tid {tid} on lane {n}")
            self.cpl_ts[n] += 1
            await self.mac_cpl[n].send(AxiStreamFrame([self.cpl_ts[n]], tid=tid))

    async def reset(self):
        dut = self.dut
        dut.rst.setimmediatevalue(0)
        for n in range(LANES):
            for nm_ in ("mac_rx_rst", "mac_tx_rst"):
                getattr(dut, f"{nm_}_{n}").setimmediatevalue(0)
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        dut.rst.value = 1
        for n in range(LANES):
            getattr(dut, f"mac_rx_rst_{n}").value = 1
            getattr(dut, f"mac_tx_rst_{n}").value = 1
        for _ in range(10):
            await RisingEdge(dut.clk)
        dut.rst.value = 0
        for n in range(LANES):
            getattr(dut, f"mac_rx_rst_{n}").value = 0
            getattr(dut, f"mac_tx_rst_{n}").value = 0
        for _ in range(10):
            await RisingEdge(dut.clk)
        # power-up table clear
        await self.regs.wait_clear()

    # ---------------------------------------------------------------- setup

    async def setup_gateway(self, punt_hdr=True, egress_en=0xff):
        """One LAN lane and two WAN lanes, as in the scope of work."""
        m = self.model
        m.enable = True
        m.bypass_mask = 0
        m.punt_hdr = punt_hdr
        m.egress_en = egress_en
        m.nh[NH_LAN] = nm.NextHop(dst_mac=LAN_HOST_MAC, src_mac=GW_MAC, lane=LAN_LANE)
        for k in range(2):
            m.nh[NH_WAN[k]] = nm.NextHop(dst_mac=WAN_GW_MAC[k], src_mac=GW_MAC + 1 + k, lane=WAN_LANES[k])
        for idx, nh in m.nh.items():
            await self.regs.write_nh(idx, nh)
        await self.regs.set_ctrl(True, punt_hdr=punt_hdr, bypass=0, egress_en=egress_en)

    async def add_session(self, lan_ip, lan_port, rem_ip, rem_port, tcp, wan):
        """Install both directions of a NAT session; returns the two keys."""
        pub_ip = PUB_IP[wan]
        pub_port = 1024 + (hash((lan_ip, lan_port, rem_ip, rem_port, tcp)) & 0x7fff)
        out_key = nm.Key(lane=LAN_LANE, vid=0, tcp=tcp, sip=lan_ip, dip=rem_ip, sport=lan_port, dport=rem_port)
        in_key = nm.Key(lane=WAN_LANES[wan], vid=0, tcp=tcp, sip=rem_ip, dip=pub_ip, sport=rem_port, dport=pub_port)
        out_e = nm.Entry(key=out_key, xlate_dst=0, new_ip=pub_ip, new_port=pub_port, dec_ttl=1, nh_idx=NH_WAN[wan])
        in_e = nm.Entry(key=in_key, xlate_dst=1, new_ip=lan_ip, new_port=lan_port, dec_ttl=1, nh_idx=NH_LAN)
        for e in (out_e, in_e):
            await self.regs.apply(self.model.table.insert(e))
        return out_key, in_key

    # ---------------------------------------------------------------- traffic

    def next_payload(self, lane, size):
        self.seq += 1
        tag = lane.to_bytes(1, 'big') + self.seq.to_bytes(7, 'big')
        return tag + bytes(random.getrandbits(8) for _ in range(max(0, size - 8)))

    def flow_packet(self, key, lane, flags='A', size=None, ttl=64, src_mac=None):
        size = random.randint(8, 200) if size is None else size
        l4 = TCP(sport=key.sport, dport=key.dport, flags=flags) if key.tcp else UDP(sport=key.sport, dport=key.dport)
        p = Ether(dst=mac_str(GW_MAC), src=mac_str(src_mac or LAN_HOST_MAC)) / \
            IP(src=ip_str(key.sip), dst=ip_str(key.dip), ttl=ttl) / l4 / Raw(self.next_payload(lane, size))
        return bytes(p)

    def exception_packet(self, lane):
        kind = random.choice(["arp", "icmp", "mcast", "frag", "ttl", "syn", "opts", "csum", "ipv6"])
        pay = Raw(self.next_payload(lane, 16))
        src = mac_str(LAN_HOST_MAC)
        if kind == "arp":
            p = Ether(dst="ff:ff:ff:ff:ff:ff", src=src) / ARP(pdst="192.168.1.1") / pay
        elif kind == "icmp":
            p = Ether(dst=mac_str(GW_MAC), src=src) / IP(src="192.168.1.10", dst="8.8.8.8") / ICMP() / pay
        elif kind == "mcast":
            p = Ether(dst="01:00:5e:00:00:fb", src=src) / IP(src="192.168.1.10", dst="224.0.0.251") / UDP(sport=5353, dport=5353) / pay
        elif kind == "frag":
            p = Ether(dst=mac_str(GW_MAC), src=src) / IP(src="192.168.1.10", dst="8.8.8.8", flags="MF") / UDP() / pay
        elif kind == "ttl":
            p = Ether(dst=mac_str(GW_MAC), src=src) / IP(src="192.168.1.10", dst="8.8.8.8", ttl=1) / UDP() / pay
        elif kind == "syn":
            p = Ether(dst=mac_str(GW_MAC), src=src) / IP(src="192.168.1.10", dst="8.8.8.8") / TCP(flags="S") / pay
        elif kind == "opts":
            p = Ether(dst=mac_str(GW_MAC), src=src) / IP(src="192.168.1.10", dst="8.8.8.8", options=b'\x01\x01\x01\x00') / UDP() / pay
        elif kind == "csum":
            p = Ether(dst=mac_str(GW_MAC), src=src) / IP(src="192.168.1.10", dst="8.8.8.8", chksum=0x1234) / UDP() / pay
        else:
            p = Ether(dst=mac_str(GW_MAC), src=src, type=0x86dd) / pay
        return bytes(p)

    async def send(self, lane, data, bad=False):
        """Send on a MAC RX lane and record the model's prediction."""
        if not bad:
            self.expect(lane, data)
        tuser = (random.getrandbits(48) << 1) | (1 if bad else 0)
        await self.mac_rx[lane].send(AxiStreamFrame(data, tuser=tuser))

    def expect(self, lane, data):
        out = self.model.process(lane, data)
        if out.kind == "fwd":
            self.exp_fwd[out.lane].append(out.data)
        else:
            self.exp_punt[lane].append(out.data)
        return out

    def begin(self):
        self.exp_fwd = [[] for _ in range(LANES)]
        self.exp_punt = [[] for _ in range(LANES)]
        self.exp_host = [[] for _ in range(LANES)]
        self.punt_rx = [[] for _ in range(LANES)]
        for n in range(LANES):
            self.fwd_rx[n].clear()
            self.host_rx[n].clear()

    async def collect(self, timeout_us=2000):
        """Wait until every expected frame has arrived, then compare."""
        async def wait_all():
            while True:
                for n in range(LANES):
                    while not self.core_rx[n].empty():
                        self.punt_rx[n].append(bytes((self.core_rx[n].recv_nowait()).tdata))
                done = all(len(self.punt_rx[n]) >= len(self.exp_punt[n]) and
                           len(self.fwd_rx[n]) >= len(self.exp_fwd[n]) and
                           len(self.host_rx[n]) >= len(self.exp_host[n]) for n in range(LANES))
                if done:
                    return
                await Timer(200, 'ns')
        await with_timeout(wait_all(), timeout_us, 'us')
        await Timer(2, 'us')
        for n in range(LANES):
            while not self.core_rx[n].empty():
                self.punt_rx[n].append(bytes((self.core_rx[n].recv_nowait()).tdata))
        self.check()

    def check(self):
        for n in range(LANES):
            # punts arrive in order per ingress lane
            assert len(self.punt_rx[n]) == len(self.exp_punt[n]), f"lane {n}: {len(self.punt_rx[n])} punts, expected {len(self.exp_punt[n])}"
            for k, (got, exp) in enumerate(zip(self.punt_rx[n], self.exp_punt[n])):
                assert got == exp, f"lane {n} punt {k} mismatch\n got {got.hex()}\n exp {exp.hex()}"
            # forwarded frames: same multiset; order per ingress lane preserved (tags carry lane + seq)
            assert len(self.fwd_rx[n]) == len(self.exp_fwd[n]), f"lane {n}: {len(self.fwd_rx[n])} forwarded, expected {len(self.exp_fwd[n])}"
            assert sorted(self.fwd_rx[n]) == sorted(self.exp_fwd[n]), f"lane {n}: forwarded frames differ"
            last = {}
            for f in self.fwd_rx[n]:
                p = Ether(f)
                tag = bytes(p[Raw].load[:8])
                src, seq = tag[0], int.from_bytes(tag[1:], 'big')
                assert seq > last.get(src, -1), f"lane {n}: reordered frames from lane {src}"
                last[src] = seq
            # host transmit frames, in order
            assert self.host_rx[n] == self.exp_host[n], f"lane {n}: host TX mismatch"


    def dump_lane_state(self):
        u = self.dut.uut
        for n in range(LANES):
            l = u.lane[n]
            def v(h):
                try:
                    return int(h.value)
                except Exception as e:
                    return f"?{e.__class__.__name__}"
            self.log.info("lane %d: rx v/r %s/%s hold_in v/r %s/%s desc v/r %s/%s key v/r %s/%s meta v %s res v %s hold_out v/r %s/%s punt v/r %s/%s fwd v/r %s/%s",
                n, v(l.rx_axis.tvalid), v(l.rx_axis.tready), v(l.hold_in_axis.tvalid), v(l.hold_in_axis.tready),
                v(l.desc_valid), v(l.desc_ready), v(u.key_valid[n]), v(u.key_ready[n]), v(l.meta_valid), v(l.lres_valid),
                v(l.hold_out_axis.tvalid), v(l.hold_out_axis.tready), v(l.punt_axis.tvalid), v(l.punt_axis.tready),
                v(u.fwd_axis[n].tvalid), v(u.fwd_axis[n].tready))

    async def check_stats(self):
        for n in range(LANES):
            for r in range(16):
                got = await self.regs.read_stat(n, r)
                exp = self.model.stats.get((n, r), 0)
                assert got == exp, f"stat lane {n} {nm.RSN_NAMES.get(r, r)}: {got} != {exp}"


def random_ip():
    return random.getrandbits(32)


def lan_ip():
    return 0xc0a80100 | random.randint(2, 254)


async def make_sessions(tb, count):
    sessions = []
    for _ in range(count):
        tcp = random.randint(0, 1)
        wan = random.randint(0, 1)
        args = (lan_ip(), random.randint(1024, 65535), random_ip(), random.choice([53, 80, 443, random.randint(1, 65535)]), tcp, wan)
        out_key, in_key = await tb.add_session(*args)
        sessions.append((out_key, in_key, wan))
    return sessions


# ------------------------------------------------------------------ tests

@cocotb.test()
async def run_test_bypass(dut):
    """After reset every lane is in bypass: stock NIC behaviour, completions filtered."""
    tb = TB(dut)
    random.seed(1)
    await tb.reset()
    tb.begin()

    assert await tb.regs.rd(0x0000) == nm_id()
    ctrl = await tb.regs.rd(0x0010)
    assert ctrl & 1 == 0 and (ctrl >> 8) & 0xff == 0xff

    for n in range(LANES):
        for _ in range(10):
            await tb.send(n, tb.flow_packet(nm.Key(n, 0, 1, lan_ip(), random_ip(), 1000, 80), n))
            await tb.send(n, tb.exception_packet(n))
        # host transmit through the merge
        for _ in range(5):
            d = bytes(tb.next_payload(n, random.randint(60, 300)))
            tb.exp_host[n].append(d)
            await tb.core_tx[n].send(AxiStreamFrame(d, tuser=0))

    await tb.collect()
    await tb.check_stats()

    # only host-frame completions reach the core
    for n in range(LANES):
        cpls = []
        while not tb.core_cpl[n].empty():
            cpls.append(tb.core_cpl[n].recv_nowait())
        assert len(cpls) == 5, f"lane {n}: {len(cpls)} completions"
        assert all((c.tid[0] if isinstance(c.tid, list) else c.tid) == 0 for c in cpls)


def nm_id():
    return 0x4E415447


@cocotb.test()
async def run_test_nat(dut):
    """Sessions installed over AXI-Lite; LAN->WAN and WAN->LAN forwarded; misses and exceptions punted."""
    tb = TB(dut)
    random.seed(2)
    await tb.reset()
    await tb.setup_gateway(punt_hdr=True)
    sessions = await make_sessions(tb, 40)
    tb.begin()

    for _ in range(6):
        for out_key, in_key, wan in sessions:
            await tb.send(LAN_LANE, tb.flow_packet(out_key, LAN_LANE))
            await tb.send(WAN_LANES[wan], tb.flow_packet(in_key, WAN_LANES[wan], src_mac=WAN_GW_MAC[wan]))
        # misses and exceptions
        for _ in range(10):
            k = nm.Key(LAN_LANE, 0, random.randint(0, 1), lan_ip(), random_ip(), random.randint(1, 65535), 443)
            await tb.send(LAN_LANE, tb.flow_packet(k, LAN_LANE))
            await tb.send(random.choice([LAN_LANE] + WAN_LANES), tb.exception_packet(LAN_LANE))
        # FIN on an offloaded flow goes to software
        out_key, in_key, wan = random.choice(sessions)
        if out_key.tcp:
            await tb.send(LAN_LANE, tb.flow_packet(out_key, LAN_LANE, flags='FA'))

    await tb.collect()
    await tb.check_stats()

    # per-entry counters for one session
    out_key = sessions[0][0]
    idx, _ = tb.model.table.lookup(out_key)
    st = await tb.regs.read_state(idx)
    assert st.valid and st.pkts == 6, st


@cocotb.test()
async def run_test_random(dut):
    """Random traffic on all lanes, VLAN-free gateway layout, table churn between bursts."""
    tb = TB(dut)
    random.seed(3)
    await tb.reset()
    await tb.setup_gateway(punt_hdr=random.choice([False, True]))
    sessions = await make_sessions(tb, 120)

    for burst in range(4):
        tb.begin()
        for n in range(LANES):
            tb.mac_rx[n].set_pause_generator(itertools.cycle([random.random() < 0.3 for _ in range(37)]))
            tb.mac_tx[n].set_pause_generator(itertools.cycle([random.random() < 0.2 for _ in range(41)]))
        sends = []
        for _ in range(600):
            r = random.random()
            out_key, in_key, wan = random.choice(sessions)
            if r < 0.4:
                sends.append((LAN_LANE, tb.flow_packet(out_key, LAN_LANE)))
            elif r < 0.75:
                sends.append((WAN_LANES[wan], tb.flow_packet(in_key, WAN_LANES[wan], src_mac=WAN_GW_MAC[wan])))
            elif r < 0.85:
                lane = random.randrange(LANES)
                sends.append((lane, tb.exception_packet(lane)))
            else:
                lane = random.randrange(LANES)
                k = nm.Key(lane, 0, random.randint(0, 1), random_ip(), random_ip(), random.randint(1, 65535), random.randint(1, 65535))
                sends.append((lane, tb.flow_packet(k, lane)))
        # host transmit traffic on the WAN lanes
        for n in WAN_LANES:
            for _ in range(20):
                d = bytes(tb.next_payload(n, random.randint(60, 600)))
                tb.exp_host[n].append(d)
                await tb.core_tx[n].send(AxiStreamFrame(d, tuser=0))
        for lane, data in sends:
            await tb.send(lane, data)
        await tb.collect()

        # churn: delete some sessions, add new ones (relocations included)
        for _ in range(15):
            out_key, in_key, wan = sessions.pop(random.randrange(len(sessions)))
            for k in (out_key, in_key):
                await tb.regs.apply(tb.model.table.delete(k))
        sessions += await make_sessions(tb, 15)
        tb.log.info("burst %d done; table load %.2f", burst, tb.model.table.load())

    await tb.check_stats()


@cocotb.test()
async def run_test_line_rate(dut):
    """Three lanes of 64-byte hits at 25G line rate: nothing dropped. Then an 8-lane flood: drops counted."""
    tb = TB(dut)
    random.seed(4)
    await tb.reset()
    await tb.setup_gateway(punt_hdr=False)
    sessions = await make_sessions(tb, 32)

    # 64-byte frame = 8 beats of 64 bits plus 20 bytes preamble/IFG and 4 bytes FCS = 3 idle beats
    tb.begin()
    for n in range(LANES):
        tb.mac_rx[n].set_pause_generator(itertools.cycle([0]*8 + [1]*3))
    count = 400
    for k in range(count):
        out_key, in_key, wan = sessions[k % len(sessions)]
        await tb.send(LAN_LANE, tb.flow_packet(out_key, LAN_LANE, size=18))
        await tb.send(WAN_LANES[wan], tb.flow_packet(in_key, WAN_LANES[wan], size=18, src_mac=WAN_GW_MAC[wan]))
        await tb.send(WAN_LANES[1-wan], tb.flow_packet(sessions[(k+1) % len(sessions)][0], WAN_LANES[1-wan], size=18))
    await tb.collect()
    for n in range(LANES):
        assert await tb.regs.read_stat(n, STAT_DROP) == 0

    # flood: every lane back to back, more than one lookup per clock is offered
    for n in range(LANES):
        tb.mac_rx[n].set_pause_generator(None)
        tb.mac_rx[n].pause = False    # clearing the generator leaves the last pause value
    sent = [0] * LANES
    frames = []
    for k in range(250):
        for n in range(LANES):
            out_key = sessions[(k + n) % len(sessions)][0]
            key = nm.Key(n, 0, out_key.tcp, out_key.sip, out_key.dip, out_key.sport, out_key.dport)
            frames.append((n, tb.flow_packet(key, n, size=18)))
    tb.begin()
    for n, d in frames:
        sent[n] += 1
        await tb.mac_rx[n].send(AxiStreamFrame(d, tuser=0))
    await Timer(30, 'us')
    for n in range(LANES):
        while not tb.core_rx[n].empty():
            tb.punt_rx[n].append(tb.core_rx[n].recv_nowait())
    drops = [await tb.regs.read_stat(n, STAT_DROP) for n in range(LANES)]
    received = [len(tb.punt_rx[n]) + len(tb.fwd_rx[n]) for n in range(LANES)]
    tb.log.info("flood: sent %s, received %s, dropped %s", sent, received, drops)
    tb.dump_lane_state()
    for n in range(LANES):
        st = [await tb.regs.read_stat(n, r) for r in (0, 1, 15, 16, 17)]
        tb.log.info("lane %d stats miss/bypass/fwd/drop/bad %s; fwd_rx %d punt_rx %d; src empty %s frames queued %d; mac_rx tready %s",
            n, st, len(tb.fwd_rx[n]), len(tb.punt_rx[n]), tb.mac_rx[n].empty(), tb.mac_rx[n].queue_occupancy_frames,
            tb.dut.mac_rx[n].tready.value)
    assert sum(received) + sum(drops) == sum(sent), "frames lost without being counted"


@cocotb.test()
async def run_test_bad_frames(dut):
    """Frames with a bad FCS are dropped at ingress and counted; good frames around them pass."""
    tb = TB(dut)
    random.seed(5)
    await tb.reset()
    await tb.setup_gateway()
    tb.begin()
    bad = [0] * LANES
    for k in range(100):
        n = random.randrange(LANES)
        if random.random() < 0.3:
            await tb.send(n, tb.exception_packet(n), bad=True)
            bad[n] += 1
        else:
            await tb.send(n, tb.exception_packet(n))
    await tb.collect()
    for n in range(LANES):
        assert await tb.regs.read_stat(n, STAT_BAD) == bad[n]


@cocotb.test()
async def run_test_events(dut):
    """FIN/RST events on hits and idle events from the scanner, read through the event registers."""
    tb = TB(dut)
    random.seed(6)
    await tb.reset()
    await tb.setup_gateway()
    sessions = await make_sessions(tb, 8)
    tcp_sessions = [s for s in sessions if s[0].tcp]
    udp_sessions = [s for s in sessions if not s[0].tcp]

    # FIN and RST on established TCP flows
    tb.begin()
    want = []
    for flags, evt in (('FA', nm.EVT_FIN), ('R', nm.EVT_RST)):
        out_key, _, _ = tcp_sessions[len(want) % len(tcp_sessions)]
        await tb.send(LAN_LANE, tb.flow_packet(out_key, LAN_LANE, flags=flags))
        want.append((evt, tb.model.table.lookup(out_key)[0]))
    await tb.collect()
    got = []
    for _ in range(50):
        e = await tb.regs.pop_event()
        if e is None:
            break
        got.append(e)
    assert [(t, i) for t, i, _ in got] == want, got

    # idle aging: UDP threshold 80 ticks of 64 cycles; touch one UDP session, the rest go idle
    await tb.regs.wr(0x0034, 80)
    await tb.regs.wr(0x0030, 1 << 30)
    live = udp_sessions[0]
    idle_idx = {tb.model.table.lookup(k)[0] for s in udp_sessions[1:] for k in s[:2]}
    seen = set()
    for _ in range(200):
        tb.begin()
        await tb.send(LAN_LANE, tb.flow_packet(live[0], LAN_LANE))
        await tb.send(WAN_LANES[live[2]], tb.flow_packet(live[1], WAN_LANES[live[2]], src_mac=WAN_GW_MAC[live[2]]))
        await tb.collect()
        while True:
            e = await tb.regs.pop_event()
            if e is None:
                break
            t, i, _ = e
            assert t == nm.EVT_IDLE, e
            assert i not in seen, f"idle event repeated for {i}"
            seen.add(i)
        if seen >= idle_idx:
            break
        await Timer(1, 'us')
    assert seen == idle_idx, (seen, idle_idx)


# ------------------------------------------------------------------ cocotb-test

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


@pytest.mark.parametrize("ram_pipe", [3, 4])
@pytest.mark.parametrize("bucket_w", [8])
def test_natgw_shim(request, bucket_w, ram_pipe):
    dut = "natgw_shim"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(taxi_src_dir, "axis", "rtl", "taxi_axis_if.sv"),
        os.path.join(taxi_src_dir, "axi", "rtl", "taxi_axil_if.sv"),
        os.path.join(rtl_dir, f"{dut}.f"),
        os.path.join(tests_dir, f"{toplevel}.sv"),
    ]

    verilog_sources = process_f_files(verilog_sources)

    parameters = {}
    parameters['BUCKET_W'] = bucket_w
    parameters['RAM_PIPE'] = ram_pipe
    parameters['TICK_DIV_RST'] = 64

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
        compile_args=["-Wno-fatal"],
    )
