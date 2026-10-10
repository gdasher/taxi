#!/usr/bin/env python
# SPDX-License-Identifier: MIT
# NAT gateway system test: fpga_core_nat with PCIe, driver and BASE-R SerDes models
"""

Copyright (c) 2020-2026 FPGA Ninja, LLC

Authors:
- Alex Forencich

"""

import itertools
import logging
import os
import sys

import pytest
import cocotb_test.simulator
from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, TCP, UDP
from scapy.packet import Raw
import random


import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer, with_timeout

from cocotbext.axi import AxiBus, AxiRam, AxiStreamBus
from cocotbext.eth import XgmiiFrame
from cocotbext.uart import UartSource, UartSink
from cocotbext.pcie.core import RootComplex
from cocotbext.pcie.xilinx.us import UltraScalePlusPcieDevice

try:
    from baser import BaseRSerdesSource, BaseRSerdesSink
    import cndm
except ImportError:
    # attempt import from current directory
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    try:
        from baser import BaseRSerdesSource, BaseRSerdesSink
        import cndm
    finally:
        del sys.path[0]

# NAT shim model and register helpers
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'lib', 'taxi', 'src', 'natgw', 'tb')))
try:
    import natgw_model as nm
    from natgw_regs import NatRegs
finally:
    del sys.path[0]

NAT_BASE = 0x800000


class TB:
    def __init__(self, dut):
        self.dut = dut

        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.DEBUG)

        # Clocks
        cocotb.start_soon(Clock(dut.clk_125mhz, 8, units="ns").start())

        # DDR tier: an AXI memory with random stalls on every channel (the
        # board's clock converter and memory controller are not modelled)
        self.ddr = int(dut.NAT_DDR.value)
        dut.ddr_calib.setimmediatevalue(1 if self.ddr else 0)
        if self.ddr:
            self.ddr_ram = AxiRam(AxiBus.from_prefix(dut, "m_axi_ddr"), dut.pcie_clk, dut.pcie_rst, size=2**16)
            for ifc in (self.ddr_ram.write_if, self.ddr_ram.read_if):
                ifc.log.setLevel(logging.WARNING)
            for i, ch in enumerate([self.ddr_ram.write_if.aw_channel, self.ddr_ram.write_if.w_channel,
                                    self.ddr_ram.write_if.b_channel, self.ddr_ram.read_if.ar_channel,
                                    self.ddr_ram.read_if.r_channel]):
                ch.set_pause_generator(itertools.cycle([0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1][i:] + [0] * i))
        else:
            for n in ("awready", "wready", "bvalid", "arready", "rvalid", "bid", "bresp", "rid", "rdata",
                      "rresp", "rlast"):
                getattr(dut, f"m_axi_ddr_{n}").setimmediatevalue(0)

        # PCIe
        self.rc = RootComplex()

        self.rc.max_payload_size = 0x1  # 256 bytes
        self.rc.max_read_request_size = 0x2  # 512 bytes

        self.dev = UltraScalePlusPcieDevice(
            # configuration options
            pcie_generation=3,
            pcie_link_width=16,
            user_clk_frequency=250e6,
            alignment="dword",
            cq_straddle=False,
            cc_straddle=False,
            rq_straddle=False,
            rc_straddle=False,
            rc_4tlp_straddle=False,
            pf_count=1,
            max_payload_size=1024,
            enable_client_tag=True,
            enable_extended_tag=True,
            enable_parity=False,
            enable_rx_msg_interface=False,
            enable_sriov=False,
            enable_extended_configuration=False,

            pf0_msi_enable=True,
            pf0_msi_count=32,
            pf1_msi_enable=False,
            pf1_msi_count=1,
            pf2_msi_enable=False,
            pf2_msi_count=1,
            pf3_msi_enable=False,
            pf3_msi_count=1,
            pf0_msix_enable=False,
            pf0_msix_table_size=31,
            pf0_msix_table_bir=4,
            pf0_msix_table_offset=0x00000000,
            pf0_msix_pba_bir=4,
            pf0_msix_pba_offset=0x00008000,
            pf1_msix_enable=False,
            pf1_msix_table_size=0,
            pf1_msix_table_bir=0,
            pf1_msix_table_offset=0x00000000,
            pf1_msix_pba_bir=0,
            pf1_msix_pba_offset=0x00000000,
            pf2_msix_enable=False,
            pf2_msix_table_size=0,
            pf2_msix_table_bir=0,
            pf2_msix_table_offset=0x00000000,
            pf2_msix_pba_bir=0,
            pf2_msix_pba_offset=0x00000000,
            pf3_msix_enable=False,
            pf3_msix_table_size=0,
            pf3_msix_table_bir=0,
            pf3_msix_table_offset=0x00000000,
            pf3_msix_pba_bir=0,
            pf3_msix_pba_offset=0x00000000,

            # signals
            # Clock and Reset Interface
            user_clk=dut.pcie_clk,
            user_reset=dut.pcie_rst,
            # user_lnk_up
            # sys_clk
            # sys_clk_gt
            # sys_reset
            # phy_rdy_out

            # Requester reQuest Interface
            rq_bus=AxiStreamBus.from_entity(dut.m_axis_pcie_rq),
            pcie_rq_seq_num0=dut.pcie_rq_seq_num0,
            pcie_rq_seq_num_vld0=dut.pcie_rq_seq_num_vld0,
            pcie_rq_seq_num1=dut.pcie_rq_seq_num1,
            pcie_rq_seq_num_vld1=dut.pcie_rq_seq_num_vld1,
            # pcie_rq_tag0
            # pcie_rq_tag1
            # pcie_rq_tag_av
            # pcie_rq_tag_vld0
            # pcie_rq_tag_vld1

            # Requester Completion Interface
            rc_bus=AxiStreamBus.from_entity(dut.s_axis_pcie_rc),

            # Completer reQuest Interface
            cq_bus=AxiStreamBus.from_entity(dut.s_axis_pcie_cq),
            # pcie_cq_np_req
            # pcie_cq_np_req_count

            # Completer Completion Interface
            cc_bus=AxiStreamBus.from_entity(dut.m_axis_pcie_cc),

            # Transmit Flow Control Interface
            # pcie_tfc_nph_av=dut.pcie_tfc_nph_av,
            # pcie_tfc_npd_av=dut.pcie_tfc_npd_av,

            # Configuration Management Interface
            cfg_mgmt_addr=dut.cfg_mgmt_addr,
            cfg_mgmt_function_number=dut.cfg_mgmt_function_number,
            cfg_mgmt_write=dut.cfg_mgmt_write,
            cfg_mgmt_write_data=dut.cfg_mgmt_write_data,
            cfg_mgmt_byte_enable=dut.cfg_mgmt_byte_enable,
            cfg_mgmt_read=dut.cfg_mgmt_read,
            cfg_mgmt_read_data=dut.cfg_mgmt_read_data,
            cfg_mgmt_read_write_done=dut.cfg_mgmt_read_write_done,
            # cfg_mgmt_debug_access

            # Configuration Status Interface
            # cfg_phy_link_down
            # cfg_phy_link_status
            # cfg_negotiated_width
            # cfg_current_speed
            cfg_max_payload=dut.cfg_max_payload,
            cfg_max_read_req=dut.cfg_max_read_req,
            # cfg_function_status
            # cfg_vf_status
            # cfg_function_power_state
            # cfg_vf_power_state
            # cfg_link_power_state
            # cfg_err_cor_out
            # cfg_err_nonfatal_out
            # cfg_err_fatal_out
            # cfg_local_error_out
            # cfg_local_error_valid
            # cfg_rx_pm_state
            # cfg_tx_pm_state
            # cfg_ltssm_state
            cfg_rcb_status=dut.cfg_rcb_status,
            # cfg_obff_enable
            # cfg_pl_status_change
            # cfg_tph_requester_enable
            # cfg_tph_st_mode
            # cfg_vf_tph_requester_enable
            # cfg_vf_tph_st_mode

            # Configuration Received Message Interface
            # cfg_msg_received
            # cfg_msg_received_data
            # cfg_msg_received_type

            # Configuration Transmit Message Interface
            # cfg_msg_transmit
            # cfg_msg_transmit_type
            # cfg_msg_transmit_data
            # cfg_msg_transmit_done

            # Configuration Flow Control Interface
            cfg_fc_ph=dut.cfg_fc_ph,
            cfg_fc_pd=dut.cfg_fc_pd,
            cfg_fc_nph=dut.cfg_fc_nph,
            cfg_fc_npd=dut.cfg_fc_npd,
            cfg_fc_cplh=dut.cfg_fc_cplh,
            cfg_fc_cpld=dut.cfg_fc_cpld,
            cfg_fc_sel=dut.cfg_fc_sel,

            # Configuration Control Interface
            # cfg_hot_reset_in
            # cfg_hot_reset_out
            # cfg_config_space_enable
            # cfg_dsn
            # cfg_bus_number
            # cfg_ds_port_number
            # cfg_ds_bus_number
            # cfg_ds_device_number
            # cfg_ds_function_number
            # cfg_power_state_change_ack
            # cfg_power_state_change_interrupt
            # cfg_err_cor_in=dut.status_error_cor,
            # cfg_err_uncor_in=dut.status_error_uncor,
            # cfg_flr_in_process
            # cfg_flr_done
            # cfg_vf_flr_in_process
            # cfg_vf_flr_func_num
            # cfg_vf_flr_done
            # cfg_pm_aspm_l1_entry_reject
            # cfg_pm_aspm_tx_l0s_entry_disable
            # cfg_req_pm_transition_l23_ready
            # cfg_link_training_enable

            # Configuration Interrupt Controller Interface
            # cfg_interrupt_int
            # cfg_interrupt_sent
            # cfg_interrupt_pending
            cfg_interrupt_msi_enable=dut.cfg_interrupt_msi_enable,
            cfg_interrupt_msi_mmenable=dut.cfg_interrupt_msi_mmenable,
            cfg_interrupt_msi_mask_update=dut.cfg_interrupt_msi_mask_update,
            cfg_interrupt_msi_data=dut.cfg_interrupt_msi_data,
            cfg_interrupt_msi_select=dut.cfg_interrupt_msi_select,
            cfg_interrupt_msi_int=dut.cfg_interrupt_msi_int,
            cfg_interrupt_msi_pending_status=dut.cfg_interrupt_msi_pending_status,
            cfg_interrupt_msi_pending_status_data_enable=dut.cfg_interrupt_msi_pending_status_data_enable,
            cfg_interrupt_msi_pending_status_function_num=dut.cfg_interrupt_msi_pending_status_function_num,
            cfg_interrupt_msi_sent=dut.cfg_interrupt_msi_sent,
            cfg_interrupt_msi_fail=dut.cfg_interrupt_msi_fail,
            # cfg_interrupt_msix_enable=dut.cfg_interrupt_msix_enable,
            # cfg_interrupt_msix_mask=dut.cfg_interrupt_msix_mask,
            # cfg_interrupt_msix_vf_enable=dut.cfg_interrupt_msix_vf_enable,
            # cfg_interrupt_msix_vf_mask=dut.cfg_interrupt_msix_vf_mask,
            # cfg_interrupt_msix_address=dut.cfg_interrupt_msix_address,
            # cfg_interrupt_msix_data=dut.cfg_interrupt_msix_data,
            # cfg_interrupt_msix_int=dut.cfg_interrupt_msix_int,
            # cfg_interrupt_msix_vec_pending=dut.cfg_interrupt_msix_vec_pending,
            # cfg_interrupt_msix_vec_pending_status=dut.cfg_interrupt_msix_vec_pending_status,
            # cfg_interrupt_msix_sent=dut.cfg_interrupt_msix_sent,
            # cfg_interrupt_msix_fail=dut.cfg_interrupt_msix_fail,
            cfg_interrupt_msi_attr=dut.cfg_interrupt_msi_attr,
            cfg_interrupt_msi_tph_present=dut.cfg_interrupt_msi_tph_present,
            cfg_interrupt_msi_tph_type=dut.cfg_interrupt_msi_tph_type,
            cfg_interrupt_msi_tph_st_tag=dut.cfg_interrupt_msi_tph_st_tag,
            cfg_interrupt_msi_function_number=dut.cfg_interrupt_msi_function_number,

            # Configuration Extend Interface
            # cfg_ext_read_received
            # cfg_ext_write_received
            # cfg_ext_register_number
            # cfg_ext_function_number
            # cfg_ext_write_data
            # cfg_ext_write_byte_enable
            # cfg_ext_read_data
            # cfg_ext_read_data_valid
        )

        # self.dev.log.setLevel(logging.DEBUG)

        self.rc.make_port().connect(self.dev)

        self.dev.functions[0].configure_bar(0, 2**int(dut.uut.cndm_inst.axil_ctrl_bar.ADDR_W))

        # UART
        self.uart_sources = []
        self.uart_sinks = []

        for sig in dut.uart_rxd:
            self.uart_sources.append(UartSource(sig, baud=3000000, bits=8, stop_bits=1))
        for sig in dut.uart_txd:
            self.uart_sinks.append(UartSink(sig, baud=3000000, bits=8, stop_bits=1))

        # Ethernet
        for clk in dut.eth_gty_mgt_refclk_p:
            cocotb.start_soon(Clock(clk, 6.4, units="ns").start())

        self.qsfp_sources = []
        self.qsfp_sinks = []

        for ch in itertools.chain.from_iterable([inst.mac_inst.ch for inst in dut.uut.gt_quad]):
            gt_inst = ch.ch_inst.gt.gt_inst

            if ch.ch_inst.DATA_W.value == 64:
                if ch.ch_inst.CFG_LOW_LATENCY.value:
                    clk = 2.482
                    gbx_cfg = (66, [64, 65])
                else:
                    clk = 2.56
                    gbx_cfg = None
            else:
                if ch.ch_inst.CFG_LOW_LATENCY.value:
                    clk = 3.102
                    gbx_cfg = (66, [64, 65])
                else:
                    clk = 3.2
                    gbx_cfg = None

            cocotb.start_soon(Clock(gt_inst.tx_clk, clk, units="ns").start())
            cocotb.start_soon(Clock(gt_inst.rx_clk, clk, units="ns").start())

            self.qsfp_sources.append(BaseRSerdesSource(
                data=gt_inst.serdes_rx_data,
                data_valid=gt_inst.serdes_rx_data_valid,
                hdr=gt_inst.serdes_rx_hdr,
                hdr_valid=gt_inst.serdes_rx_hdr_valid,
                clock=gt_inst.rx_clk,
                slip=gt_inst.serdes_rx_bitslip,
                reverse=True,
                gbx_cfg=gbx_cfg
            ))
            self.qsfp_sinks.append(BaseRSerdesSink(
                data=gt_inst.serdes_tx_data,
                data_valid=gt_inst.serdes_tx_data_valid,
                hdr=gt_inst.serdes_tx_hdr,
                hdr_valid=gt_inst.serdes_tx_hdr_valid,
                gbx_sync=gt_inst.serdes_tx_gbx_sync,
                clock=gt_inst.tx_clk,
                reverse=True,
                gbx_cfg=gbx_cfg
            ))

        dut.sw.setimmediatevalue(0)
        dut.eth_port_modprsl.setimmediatevalue(0)
        dut.eth_port_intl.setimmediatevalue(0)

        self.loopback_enable = False
        cocotb.start_soon(self._run_loopback())

    async def init(self):

        self.dut.rst_125mhz.setimmediatevalue(0)

        await FallingEdge(self.dut.pcie_rst)
        await Timer(100, 'ns')

        for k in range(10):
            await RisingEdge(self.dut.clk_125mhz)

        self.dut.rst_125mhz.value = 1

        for k in range(10):
            await RisingEdge(self.dut.clk_125mhz)

        self.dut.rst_125mhz.value = 0

        for k in range(10):
            await RisingEdge(self.dut.clk_125mhz)

        await self.rc.enumerate()

    async def _run_loopback(self):
        while True:
            await RisingEdge(self.dut.pcie_clk)

            if self.loopback_enable:
                for src, snk in zip(self.qsfp_sources, self.qsfp_sinks):
                    while not snk.empty():
                        await src.send(await snk.recv())

@cocotb.test()
async def run_test_bypass(dut):
    """Stock cndm test, unchanged: after reset the NAT shim is fully transparent."""

    tb = TB(dut)
    dut.cfg_flr_in_process.setimmediatevalue(0)

    await tb.init()

    tb.log.info("Init driver model")
    driver = cndm.Driver()
    await driver.init_pcie_dev(tb.rc.find_device(tb.dev.functions[0].pcie_id))

    tb.log.info("Init complete")

    tb.log.info("Wait for block lock")
    for k in range(1200):
        await RisingEdge(tb.dut.clk_125mhz)

    for snk in tb.qsfp_sinks:
        snk.clear()

    tb.log.info("Send and receive single packet on each port")

    for k in range(len(driver.ports)):
        data = f"Corundum rocks on port {k}!".encode('ascii')

        await driver.ports[k].start_xmit(data)

        pkt = await tb.qsfp_sinks[k].recv()
        tb.log.info("Got TX packet: %s", pkt)

        assert pkt.get_payload() == data.ljust(60, b'\x00')
        assert pkt.check_fcs()

        await tb.qsfp_sources[k].send(pkt)

        pkt = await driver.ports[k].recv()
        tb.log.info("Got RX packet: %s", pkt)

        assert bytes(pkt) == data.ljust(60, b'\x00')

    tb.log.info("Multiple small packets")

    count = 64
    pkts = [bytearray([(x+k) % 256 for x in range(60)]) for k in range(count)]

    tb.loopback_enable = True

    for p in pkts:
        await driver.ports[0].start_xmit(p)

    for k in range(count):
        pkt = await driver.ports[0].recv()

        tb.log.info("Got RX packet: %s", pkt)

        assert bytes(pkt) == pkts[k].ljust(60, b'\x00')

    tb.loopback_enable = False

    tb.log.info("Multiple large packets")

    count = 64
    pkts = [bytearray([(x+k) % 256 for x in range(1514)]) for k in range(count)]

    tb.loopback_enable = True

    for p in pkts:
        await driver.ports[0].start_xmit(p)

    for k in range(count):
        pkt = await driver.ports[0].recv()

        tb.log.info("Got RX packet: %s", pkt)

        assert bytes(pkt) == pkts[k].ljust(60, b'\x00')

    tb.loopback_enable = False

    await RisingEdge(dut.clk_125mhz)
    await RisingEdge(dut.clk_125mhz)




LAN_LANE = 0
WAN_LANES = [1, 2]
GW_MAC = 0x02000000aa00
LAN_HOST_MAC = 0x020000001100
WAN_GW_MAC = [0x020000002200, 0x020000003300]
PUB_IP = [0xc6336401, 0xcb007101]
NH_LAN = 1
NH_WAN = [2, 3]


def mac_str(v):
    return ':'.join(f'{b:02x}' for b in v.to_bytes(6, 'big'))


def ip_str(v):
    return '.'.join(str(b) for b in v.to_bytes(4, 'big'))


class NatHarness:
    """Drives the NAT block through BAR0 exactly as the driver will."""

    def __init__(self, tb, driver):
        self.tb = tb
        self.driver = driver
        self.regs = NatRegs(driver.hw_regs.read_dword, driver.hw_regs.write_dword, base=NAT_BASE)
        self.model = None
        self.seq = 0

    async def setup(self, punt_hdr=True):
        tb = self.tb
        assert await self.regs.rd(0x0000) == 0x4E415447
        caps = await self.regs.caps()
        tb.log.info("NAT caps: %s", caps)
        await self.regs.wait_clear()
        st = await self.regs.ddr_status()
        self.ddr = st["present"] and st["calibrated"]
        self.model = nm.ShimModel(bucket_w=caps["idx_w"] - 3, ddr_bucket_w=st["bucket_w"] if self.ddr else None)
        if self.ddr:
            await self.regs.ddr_clear(enable_after=True)
            self.model.ddr_active = True
        m = self.model
        m.enable = True
        m.bypass_mask = 0
        m.punt_hdr = punt_hdr
        m.nh[NH_LAN] = nm.NextHop(dst_mac=LAN_HOST_MAC, src_mac=GW_MAC, lane=LAN_LANE)
        for k in range(2):
            m.nh[NH_WAN[k]] = nm.NextHop(dst_mac=WAN_GW_MAC[k], src_mac=GW_MAC + 1 + k, lane=WAN_LANES[k])
        for idx, nh in m.nh.items():
            await self.regs.write_nh(idx, nh)
        await self.regs.set_ctrl(True, punt_hdr=punt_hdr, bypass=0)

    async def add_session(self, lan_ip, lan_port, rem_ip, rem_port, tcp, wan, tier="uram"):
        pub_ip = PUB_IP[wan]
        pub_port = 20000 + (self.seq % 30000)
        self.seq += 1
        out_key = nm.Key(LAN_LANE, 0, tcp, lan_ip, rem_ip, lan_port, rem_port)
        in_key = nm.Key(WAN_LANES[wan], 0, tcp, rem_ip, pub_ip, rem_port, pub_port)
        for e in (nm.Entry(out_key, 0, pub_ip, pub_port, 1, NH_WAN[wan]),
                  nm.Entry(in_key, 1, lan_ip, lan_port, 1, NH_LAN)):
            if tier == "ddr":
                await self.regs.apply_ddr(self.model.ddr.insert(e))
            else:
                await self.regs.apply(self.model.table.insert(e))
        return out_key, in_key

    async def remove_session(self, keys, tier="uram"):
        for k in keys:
            if tier == "ddr":
                await self.regs.apply_ddr(self.model.ddr.delete(k))
            else:
                await self.regs.apply(self.model.table.delete(k))

    def packet(self, key, flags='A', size=64, src_mac=LAN_HOST_MAC):
        self.seq += 1
        pay = self.seq.to_bytes(8, 'big') + bytes(random.getrandbits(8) for _ in range(size))
        l4 = TCP(sport=key.sport, dport=key.dport, flags=flags) if key.tcp else UDP(sport=key.sport, dport=key.dport)
        return bytes(Ether(dst=mac_str(GW_MAC), src=mac_str(src_mac)) /
                     IP(src=ip_str(key.sip), dst=ip_str(key.dip), ttl=64) / l4 / Raw(pay))


async def recv_port(driver, port, timeout_us=200):
    return bytes(await with_timeout(driver.ports[port].recv(), timeout_us, 'us'))


async def recv_wire(tb, lane, timeout_us=200):
    pkt = await with_timeout(tb.qsfp_sinks[lane].recv(), timeout_us, 'us')
    assert pkt.check_fcs()
    return bytes(pkt.get_payload())


async def function_level_reset(tb):
    """pulse a function-level reset and wait for it to complete"""
    dut = tb.dut
    dut.cfg_flr_in_process.value = 1
    for k in range(20000):
        await RisingEdge(dut.pcie_clk)
        if int(dut.cfg_flr_done.value) & 1:
            break
    else:
        assert False, "cfg_flr_done never pulsed"
    dut.cfg_flr_in_process.value = 0
    for k in range(100):
        await RisingEdge(dut.pcie_clk)


async def init_all(dut):
    tb = TB(dut)
    dut.cfg_flr_in_process.setimmediatevalue(0)
    await tb.init()
    # every test starts from a function-level reset, as a host does when it
    # opens the device (vfio-pci resets it). The tests share one simulation;
    # without the reset, re-initialising the driver's queues on the running
    # NIC made it redeliver the previous test's last received frame with a
    # 4096-byte length as the first frame (seen after run_test_nat)
    await function_level_reset(tb)
    driver = cndm.Driver()
    await driver.init_pcie_dev(tb.rc.find_device(tb.dev.functions[0].pcie_id))
    for k in range(1200):
        await RisingEdge(tb.dut.clk_125mhz)
    for snk in tb.qsfp_sinks:
        snk.clear()
    # tests share one simulation: frames an earlier test left in the NIC's
    # receive path reach the new driver's queues; drop them
    for port in driver.ports:
        while not port.rx_queue.empty():
            stale = port.rx_queue.get_nowait()
            tb.log.info("dropped a frame left from an earlier test: %s", bytes(stale)[:16].hex())
    return tb, driver


async def debug_dump(tb, driver, nat, key):
    for lane in range(8):
        st = {nm.RSN_NAMES.get(r, r): await nat.regs.read_stat(lane, r) for r in range(18)}
        st = {k: v for k, v in st.items() if v}
        tb.log.info("lane %d stats %s, host rxq %d, wire sink %d", lane, st, driver.ports[lane].rx_queue.qsize(), tb.qsfp_sinks[lane].count())
    idx, e = nat.model.table.lookup(key)
    tb.log.info("model idx %s entry %s", idx, e)
    if idx is not None:
        tb.log.info("hw entry %s", await nat.regs.read_entry(idx))
        tb.log.info("hw state %s", await nat.regs.read_state(idx))
    tb.log.info("ctrl %08x", await nat.regs.rd(0x0010))
    if not driver.ports[0].rx_queue.empty():
        tb.log.info("host port 0 got %s", bytes(driver.ports[0].rx_queue.get_nowait()).hex())


@cocotb.test()
async def run_test_nat(dut):
    """Sessions installed over BAR0: hits forwarded wire to wire with no host involvement,
    misses and FIN punted to the driver with a punt header, host TX merged, counters and events read back."""
    random.seed(10)
    tb, driver = await init_all(dut)
    nat = NatHarness(tb, driver)
    await nat.setup(punt_hdr=True)
    m = nat.model

    sessions = []
    for k in range(6):
        sessions.append(await nat.add_session(0xc0a80100 + 10 + k, 40000 + k, 0x08080800 + k, 443 if k % 2 else 53, k % 2, k % 2))

    tb.log.info("LAN -> WAN and WAN -> LAN, forwarded in hardware")
    for out_key, in_key in sessions:
        wan = WAN_LANES.index(in_key.lane)
        for lane, key, src in ((LAN_LANE, out_key, LAN_HOST_MAC), (in_key.lane, in_key, WAN_GW_MAC[wan])):
            data = nat.packet(key, src_mac=src)
            exp = m.process(lane, data)
            assert exp.kind == "fwd"
            await tb.qsfp_sources[lane].send(XgmiiFrame.from_payload(data))
            try:
                got = await recv_wire(tb, exp.lane)
            except Exception:
                await debug_dump(tb, driver, nat, key)
                raise
            assert got == exp.data, f"forwarded frame mismatch\n got {got.hex()}\n exp {exp.data.hex()}"
            parsed = Ether(got)
            tb.log.info("fwd lane %d -> %d: %s", lane, exp.lane, parsed.summary())
    await Timer(2, 'us')
    for port in range(len(driver.ports)):
        assert driver.ports[port].rx_queue.empty(), f"host saw a packet on port {port}"

    tb.log.info("miss punted to the host with a punt header")
    miss_key = nm.Key(LAN_LANE, 0, 1, 0xc0a80163, 0x01010101, 5555, 80)
    data = nat.packet(miss_key)
    exp = m.process(LAN_LANE, data)
    assert exp.kind == "punt" and exp.reason == nm.RSN_MISS
    await tb.qsfp_sources[LAN_LANE].send(XgmiiFrame.from_payload(data))
    got = await recv_port(driver, LAN_LANE)
    assert got == exp.data, f"punt mismatch\n got {got.hex()}\n exp {exp.data.hex()}"
    assert got[:2] == b'\x4e\x47' and got[3] == nm.RSN_MISS and got[16:] == data

    tb.log.info("FIN on an offloaded TCP flow goes to software and raises an event")
    out_key = sessions[1][0]
    assert out_key.tcp
    data = nat.packet(out_key, flags='FA')
    exp = m.process(LAN_LANE, data)
    assert exp.reason == nm.RSN_FINRST and exp.hit_idx is not None
    await tb.qsfp_sources[LAN_LANE].send(XgmiiFrame.from_payload(data))
    got = await recv_port(driver, LAN_LANE)
    assert got == exp.data
    evt = await nat.regs.pop_event()
    assert evt is not None and evt[0] == nm.EVT_FIN and evt[1] == exp.hit_idx, evt

    tb.log.info("host transmit on a WAN lane while hardware forwards to it")
    out_key = sessions[0][0]
    wan_lane = sessions[0][1].lane
    host = [bytes(random.getrandbits(8) for _ in range(random.randint(60, 200))) for _ in range(8)]
    fwd = []
    for h in host:
        await driver.ports[wan_lane].start_xmit(h)
        d = nat.packet(out_key)
        e = m.process(LAN_LANE, d)
        fwd.append(e.data)
        await tb.qsfp_sources[LAN_LANE].send(XgmiiFrame.from_payload(d))
    got = [await recv_wire(tb, wan_lane) for _ in range(len(host) + len(fwd))]
    assert sorted(got) == sorted([h.ljust(60, b'\x00') for h in host] + fwd)

    tb.log.info("counters")
    for lane in range(8):
        for r in range(16):
            assert await nat.regs.read_stat(lane, r) == m.stats.get((lane, r), 0), (lane, r)
    idx, _ = m.table.lookup(out_key)
    st = await nat.regs.read_state(idx)
    tb.log.info("entry %d state: %s", idx, st)
    assert st.valid and st.pkts == 1 + len(host)

    tb.log.info("idle expiry event")
    await nat.regs.wr(0x0030, 50)     # TCP threshold, ticks of 64 cycles
    await nat.regs.wr(0x0034, 50)     # UDP threshold
    idle = set()
    want = set(i for i, e in m.table.slots.items())
    for _ in range(100):
        await Timer(2, 'us')
        while True:
            e = await nat.regs.pop_event()
            if e is None:
                break
            if e[0] == nm.EVT_IDLE:
                idle.add(e[1])
        if idle >= want:
            break
    assert idle == want, (sorted(idle), sorted(want))

    await RisingEdge(dut.clk_125mhz)


def describe(data):
    """one line: punt header fields, then the frame's addresses and ports"""
    hdr = ""
    if data[:2] == b"NG":
        hdr = f"punt reason {data[3]} lane {data[4]} flags {data[5]:#x} idx {int.from_bytes(data[12:16], 'big'):#x} | "
        data = data[16:]
    try:
        e = Ether(data)
        if IP in e:
            l4 = e[TCP] if TCP in e else e[UDP] if UDP in e else None
            ports = f" {l4.sport}->{l4.dport}" if l4 is not None else ""
            return hdr + f"{e[IP].src}->{e[IP].dst}{ports} ttl {e[IP].ttl} len {len(data)}"
        return hdr + f"type {e.type:#06x} len {len(data)}"
    except Exception:
        return hdr + f"raw {data[:16].hex()} len {len(data)}"


def diff_report(tb, where, got, want):
    from collections import Counter
    g, w = Counter(got), Counter(want)
    extra, missing = list((g - w).elements()), list((w - g).elements())
    if extra or missing:
        for d in extra[:4]:
            tb.log.error("%s: unexpected  %s", where, describe(d))
        for d in missing[:4]:
            tb.log.error("%s: missing     %s", where, describe(d))
    assert not extra and not missing, \
        f"{where}: {len(extra)} unexpected and {len(missing)} missing of {len(want)} expected frames " \
        f"(first unexpected: {describe(extra[0]) if extra else '-'}; first missing: " \
        f"{describe(missing[0]) if missing else '-'})"


async def exchange(tb, driver, nat, items):
    """send (lane, frame) items, receive every expected output and compare
    with the model per destination (outputs from different ingress lanes
    may interleave at one egress lane)"""
    m = nat.model
    # Ethernet frames are at least 60 bytes (without FCS); the MACs pad
    # shorter ones, so send them padded
    items = [(lane, data.ljust(60, b"\0")) for lane, data in items]
    want_wire = {lane: [] for lane in range(8)}
    want_host = {lane: [] for lane in range(8)}
    for lane, data in items:
        exp = m.process(lane, data)
        (want_wire if exp.kind == "fwd" else want_host)[exp.lane].append(exp.data)
    for lane, data in items:
        await tb.qsfp_sources[lane].send(XgmiiFrame.from_payload(data))
    for lane in range(8):
        got = [await recv_wire(tb, lane, timeout_us=2000) for _ in want_wire[lane]]
        diff_report(tb, f"wire lane {lane}", got, want_wire[lane])
        got = [await recv_port(driver, lane, timeout_us=2000) for _ in want_host[lane]]
        diff_report(tb, f"host port {lane}", got, want_host[lane])
    await Timer(5, 'us')
    for lane in range(8):
        assert tb.qsfp_sinks[lane].empty(), f"unexpected frame on wire lane {lane}"
        assert driver.ports[lane].rx_queue.empty(), f"unexpected punt on port {lane}"
    return sum(map(len, want_wire.values())), sum(map(len, want_host.values()))


async def check_counters(nat):
    m = nat.model
    for lane in range(8):
        for r in range(18):
            assert await nat.regs.read_stat(lane, r) == m.stats.get((lane, r), 0), (lane, r)


async def check_ddr_activity(nat):
    words = (nat.model.ddr.size + 63) // 64
    for w in range(words):
        assert await nat.regs.read_activity(w) == nat.model.read_activity(w), f"activity word {w}"


@cocotb.test()
async def run_test_ddr(dut):
    """DDR tier at system level: sessions in DDR forwarded wire to wire both
    ways, a FIN on a DDR flow punted with the DDR hit index, activity bits and
    DDR statistics as the model says."""
    random.seed(13)
    tb, driver = await init_all(dut)
    if not tb.ddr:
        tb.log.info("no DDR tier in this configuration")
        return
    nat = NatHarness(tb, driver)
    await nat.setup(punt_hdr=True)
    st = await nat.regs.ddr_status()
    assert st["present"] and st["calibrated"] and st["active"], st
    m = nat.model
    sessions = []
    for k in range(8):
        tier = "ddr" if k % 2 == 0 else "uram"
        sessions.append(await nat.add_session(0xc0a80200 + k, 41000 + k, 0x09090900 + k, 443, 1, k % 2, tier) +
                        (tier,))
    items = []
    for out_key, in_key, tier in sessions:
        wan = WAN_LANES.index(in_key.lane)
        items.append((LAN_LANE, nat.packet(out_key)))
        items.append((in_key.lane, nat.packet(in_key, src_mac=WAN_GW_MAC[wan])))
    fwd, punt = await exchange(tb, driver, nat, items)
    assert fwd == len(items) and punt == 0
    out_key = sessions[0][0]
    data = nat.packet(out_key, flags='FA')
    exp = m.process(LAN_LANE, data)
    assert exp.kind == "punt" and exp.hit_idx is not None and exp.hit_idx & nm.DDR_IDX_FLAG
    await tb.qsfp_sources[LAN_LANE].send(XgmiiFrame.from_payload(data))
    got = await recv_port(driver, LAN_LANE)
    assert got == exp.data, describe(got)
    assert int.from_bytes(got[12:16], "big") == exp.hit_idx
    await check_counters(nat)
    await check_ddr_activity(nat)
    stats = await nat.regs.ddr_stats()
    assert stats["hits"] > 0 and stats["skips"] == 0 and stats["read_errors"] == 0, stats


@cocotb.test()
async def run_test_soak(dut):
    """Long random run against the model: sessions on chip (and in DDR when
    the build has the tier) with table churn between bursts, mixed traffic on
    all eight lanes (hits both ways, FIN/RST, SYN, TTL expiry, bad IP
    checksums, non-IPv4, misses), every frame's outcome compared with the C
    model, then every counter and the DDR activity bits."""
    random.seed(14)
    tb, driver = await init_all(dut)
    nat = NatHarness(tb, driver)
    await nat.setup(punt_hdr=True)
    sessions = []

    async def add(n):
        for _ in range(n):
            tier = "ddr" if nat.ddr and random.random() < 0.5 else "uram"
            try:
                o, i = await nat.add_session(0xc0a80000 | random.getrandbits(16), random.randrange(1024, 65536),
                                             random.getrandbits(32), random.choice([53, 80, 123, 443]),
                                             random.randint(0, 1), random.randint(0, 1), tier)
            except nm.TableFull:
                continue
            sessions.append((o, i, tier))

    def frame():
        r = random.random()
        o, i, tier = random.choice(sessions)
        lan = random.random() < 0.5
        key, lane, src = (o, LAN_LANE, LAN_HOST_MAC) if lan else (i, i.lane, WAN_GW_MAC[WAN_LANES.index(i.lane)])
        if r < 0.55:
            flags = random.choice(['A'] * 8 + ['PA', 'FA', 'R']) if key.tcp else 'A'
            return lane, nat.packet(key, flags=flags, size=random.randint(0, 200), src_mac=src)
        if r < 0.62 and key.tcp:
            return lane, nat.packet(key, flags='S', src_mac=src)
        if r < 0.70:
            d = bytearray(nat.packet(key, src_mac=src))
            d[14 + 8] = 1                      # TTL 1 (checksum left stale: a checksum punt or TTL)
            return lane, bytes(d)
        if r < 0.78:
            d = bytearray(nat.packet(key, src_mac=src))
            d[14 + 10] ^= 0x5a                 # bad IP header checksum
            return lane, bytes(d)
        if r < 0.84:
            return random.randrange(8), bytes(Ether(dst=mac_str(GW_MAC), src=mac_str(src), type=0x0806) /
                                              Raw(bytes(random.getrandbits(8) for _ in range(46))))
        miss = nm.Key(random.randrange(8), 0, random.randint(0, 1), random.getrandbits(32), random.getrandbits(32),
                      random.getrandbits(16), random.getrandbits(16))
        return miss.lane, nat.packet(miss, size=random.randint(0, 100))

    await add(40)
    fwd = punt = 0
    for burst in range(6):
        items = [frame() for _ in range(150)]
        f, p = await exchange(tb, driver, nat, items)
        fwd += f
        punt += p
        # churn: retire some sessions, add new ones (relocations included)
        for o, i, tier in random.sample(sessions, 5):
            await nat.remove_session((o, i), tier)
            sessions.remove((o, i, tier))
        await add(8)
        tb.log.info("burst %d: %d forwarded, %d punted; %d sessions", burst, f, p, len(sessions))
    assert fwd > 300 and punt > 150
    await check_counters(nat)
    if nat.ddr:
        await check_ddr_activity(nat)
        stats = await nat.regs.ddr_stats()
        assert stats["skips"] == 0 and stats["read_errors"] == 0, stats


@cocotb.test()
async def run_test_flr(dut):
    """Function-level reset: core and shim reset, tables cleared, lanes back in bypass, driver re-initializes."""
    random.seed(11)
    tb, driver = await init_all(dut)
    nat = NatHarness(tb, driver)
    await nat.setup(punt_hdr=False)
    out_key, in_key = await nat.add_session(0xc0a8010a, 40000, 0x08080808, 53, 0, 0)
    data = nat.packet(out_key)
    exp = nat.model.process(LAN_LANE, data)
    await tb.qsfp_sources[LAN_LANE].send(XgmiiFrame.from_payload(data))
    assert await recv_wire(tb, exp.lane) == exp.data

    tb.log.info("assert cfg_flr_in_process")
    dut.cfg_flr_in_process.value = 1
    done = False
    for k in range(20000):
        await RisingEdge(dut.pcie_clk)
        if int(dut.cfg_flr_done.value) & 1:
            done = True
            tb.log.info("cfg_flr_done after %d cycles", k)
            break
    assert done, "cfg_flr_done never pulsed"
    await RisingEdge(dut.pcie_clk)
    assert int(dut.cfg_flr_done.value) == 0, "cfg_flr_done must be a single-cycle pulse"
    dut.cfg_flr_in_process.value = 0
    for k in range(100):
        await RisingEdge(dut.pcie_clk)

    tb.log.info("NAT state after FLR")
    regs = nat.regs
    assert await regs.rd(0x0010) == 0x00ffff00, hex(await regs.rd(0x0010))
    idx, _ = nat.model.table.lookup(out_key)
    e = await regs.read_entry(idx)
    assert not e.valid
    nh = await regs.read_nh(NH_WAN[0])
    assert not nh.valid
    for lane in range(8):
        for r in range(18):
            assert await regs.read_stat(lane, r) == 0

    tb.log.info("driver re-initializes after FLR; the same flow is now a plain host packet")
    driver = cndm.Driver()
    await driver.init_pcie_dev(tb.rc.find_device(tb.dev.functions[0].pcie_id))
    for k in range(200):
        await RisingEdge(tb.dut.clk_125mhz)
    await tb.qsfp_sources[LAN_LANE].send(XgmiiFrame.from_payload(data))
    got = await recv_port(driver, LAN_LANE)
    assert got == data
    await driver.ports[3].start_xmit(data)
    assert await recv_wire(tb, 3) == data

    await RisingEdge(dut.clk_125mhz)


# cocotb-test

tests_dir = os.path.abspath(os.path.dirname(__file__))
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


@pytest.mark.parametrize("ddr", [0, 1])
@pytest.mark.parametrize("mac_data_w", [64])
def test_fpga_core_nat(request, mac_data_w, ddr):
    dut = "fpga_core_nat"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = module

    verilog_sources = [
        os.path.join(tests_dir, f"{toplevel}.sv"),
        os.path.join(rtl_dir, f"{dut}.sv"),
        os.path.join(taxi_src_dir, "natgw", "rtl", "natgw_cndm_micro_pcie_us.f"),
        os.path.join(taxi_src_dir, "natgw", "rtl", "natgw_shim.f"),
        os.path.join(taxi_src_dir, "eth", "rtl", "us", "taxi_eth_mac_25g_us.f"),
        os.path.join(taxi_src_dir, "xfcp", "rtl", "taxi_xfcp_if_uart.f"),
        os.path.join(taxi_src_dir, "xfcp", "rtl", "taxi_xfcp_switch.sv"),
        os.path.join(taxi_src_dir, "xfcp", "rtl", "taxi_xfcp_mod_apb.f"),
        os.path.join(taxi_src_dir, "xfcp", "rtl", "taxi_xfcp_mod_stats.f"),
        os.path.join(taxi_src_dir, "axis", "rtl", "taxi_axis_async_fifo.f"),
        os.path.join(taxi_src_dir, "sync", "rtl", "taxi_sync_reset.sv"),
        os.path.join(taxi_src_dir, "sync", "rtl", "taxi_sync_signal.sv"),
        os.path.join(taxi_src_dir, "io", "rtl", "taxi_debounce_switch.sv"),
        os.path.join(taxi_src_dir, "pyrite", "rtl", "pyrite_pcie_us_vpd_qspi.f"),
    ]

    verilog_sources = process_f_files(verilog_sources)

    parameters = {}

    parameters['SIM'] = "1'b1"
    parameters['VENDOR'] = "\"XILINX\""
    parameters['FAMILY'] = "\"virtexuplus\""

    parameters['SW_CNT'] = 4
    parameters['LED_CNT'] = 3
    parameters['UART_CNT'] = 1
    parameters['PORT_CNT'] = 2
    parameters['PORT_LED_CNT'] = parameters['PORT_CNT']
    parameters['GTY_QUAD_CNT'] = parameters['PORT_CNT']
    parameters['GTY_CNT'] = parameters['GTY_QUAD_CNT']*4
    parameters['GTY_CLK_CNT'] = parameters['GTY_QUAD_CNT']

    # PTP configuration
    parameters['PTP_TS_EN'] = 1
    parameters['PTP_CLK_PER_NS_NUM'] = 32
    parameters['PTP_CLK_PER_NS_DEN'] = 5

    # AXI lite interface configuration (control)
    parameters['AXIL_CTRL_DATA_W'] = 32
    parameters['AXIL_CTRL_ADDR_W'] = 24

    # MAC configuration
    parameters['CFG_LOW_LATENCY'] = 1
    parameters['COMBINED_MAC_PCS'] = 1
    parameters['MAC_DATA_W'] = mac_data_w

    # NAT shim (small table for simulation speed)
    parameters['NAT_BUCKET_W'] = 8
    parameters['NAT_RAM_PIPE'] = 3
    parameters['NAT_TICK_DIV'] = 64
    parameters['NAT_DDR'] = ddr
    parameters['NAT_DDR_BUCKET_W'] = 6

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
