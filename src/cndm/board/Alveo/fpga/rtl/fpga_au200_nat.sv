// SPDX-License-Identifier: MIT
/*

Copyright (c) 2014-2026 FPGA Ninja, LLC

Authors:
- Alex Forencich

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

/*
 * FPGA top-level module (NAT gateway build: fpga_core_nat, FLR acknowledged)
 */
module fpga #
(
    // simulation (set to avoid vendor primitives)
    parameter logic SIM = 1'b0,
    // vendor ("GENERIC", "XILINX", "ALTERA")
    parameter string VENDOR = "XILINX",
    // device family
    parameter string FAMILY = "virtexuplus",

    // FW ID
    parameter FPGA_ID = 32'h4B37093,
    parameter FW_ID = 32'h0000C001,
    parameter FW_VER = 32'h000_01_000,
    parameter BOARD_ID = 32'h10ee_90c8,
    parameter BOARD_VER = 32'h001_00_000,
    parameter BUILD_DATE = 32'd602976000,
    parameter GIT_HASH = 32'h5f87c2e8,
    parameter RELEASE_INFO = 32'h00000000,

    // PTP configuration
    parameter logic PTP_TS_EN = 1'b1,

    // AXI lite interface configuration (control)
    parameter AXIL_CTRL_DATA_W = 32,
    parameter AXIL_CTRL_ADDR_W = 24,

    // MAC configuration
    parameter logic CFG_LOW_LATENCY = 1'b1,
    parameter logic COMBINED_MAC_PCS = 1'b1,
    parameter MAC_DATA_W = 64,

    // NAT shim configuration
    parameter NAT_BUCKET_W = 16,
    parameter NAT_RAM_PIPE = 3,
    // DDR tier (builds with `define NAT_DDR): 2 x 2^NAT_DDR_BUCKET_W
    // 64-byte lines of two entries in DDR4 channel C2
    parameter NAT_DDR_BUCKET_W = 20
)
(
    /*
     * Reset: Push button, active low
     */
    input  wire logic         reset,

    /*
     * GPIO
     */
    input  wire logic [3:0]   sw,
    output wire logic [2:0]   led,

    /*
     * I2C for board management
     */
    inout  wire logic         i2c_scl,
    inout  wire logic         i2c_sda,

    /*
     * UART
     */
    output wire logic         uart_txd,
    input  wire logic         uart_rxd,

    /*
     * Ethernet: QSFP28
     */
    output wire logic         qsfp0_tx_p[4],
    output wire logic         qsfp0_tx_n[4],
    input  wire logic         qsfp0_rx_p[4],
    input  wire logic         qsfp0_rx_n[4],
    input  wire logic         qsfp0_mgt_refclk_0_p,
    input  wire logic         qsfp0_mgt_refclk_0_n,
    // input  wire logic         qsfp0_mgt_refclk_1_p,
    // input  wire logic         qsfp0_mgt_refclk_1_n,
    output wire logic         qsfp0_modsell,
    output wire logic         qsfp0_resetl,
    input  wire logic         qsfp0_modprsl,
    input  wire logic         qsfp0_intl,
    output wire logic         qsfp0_lpmode,
    // output wire logic         qsfp0_refclk_reset,
    // output wire logic [1:0]   qsfp0_fs,

    output wire logic         qsfp1_tx_p[4],
    output wire logic         qsfp1_tx_n[4],
    input  wire logic         qsfp1_rx_p[4],
    input  wire logic         qsfp1_rx_n[4],
    input  wire logic         qsfp1_mgt_refclk_0_p,
    input  wire logic         qsfp1_mgt_refclk_0_n,
    // input  wire logic         qsfp1_mgt_refclk_1_p,
    // input  wire logic         qsfp1_mgt_refclk_1_n,
    output wire logic         qsfp1_modsell,
    output wire logic         qsfp1_resetl,
    input  wire logic         qsfp1_modprsl,
    input  wire logic         qsfp1_intl,
    output wire logic         qsfp1_lpmode,
    // output wire logic         qsfp1_refclk_reset,
    // output wire logic [1:0]   qsfp1_fs,

    /*
     * PCIe
     */
    input  wire logic [15:0]  pcie_rx_p,
    input  wire logic [15:0]  pcie_rx_n,
    output wire logic [15:0]  pcie_tx_p,
    output wire logic [15:0]  pcie_tx_n,
    input  wire logic         pcie_refclk_p,
    input  wire logic         pcie_refclk_n,
    input  wire logic         pcie_reset_n
`ifdef NAT_DDR
    ,
    /*
     * DDR4 channel C2 (one RDIMM): the NAT DDR tier
     */
    input  wire logic         clk_ddr4_c2_p,
    input  wire logic         clk_ddr4_c2_n,
    output wire logic         ddr4_c2_act_n,
    output wire logic [16:0]  ddr4_c2_adr,
    output wire logic [1:0]   ddr4_c2_ba,
    output wire logic [1:0]   ddr4_c2_bg,
    output wire logic [0:0]   ddr4_c2_ck_t,
    output wire logic [0:0]   ddr4_c2_ck_c,
    output wire logic [0:0]   ddr4_c2_cke,
    output wire logic [0:0]   ddr4_c2_cs_n,
    output wire logic [0:0]   ddr4_c2_odt,
    output wire logic         ddr4_c2_par,
    output wire logic         ddr4_c2_reset_n,
    inout  wire logic [71:0]  ddr4_c2_dq,
    inout  wire logic [17:0]  ddr4_c2_dqs_t,
    inout  wire logic [17:0]  ddr4_c2_dqs_c
`endif
);

// Clock and reset
wire pcie_user_clk;
wire pcie_user_rst;

wire clk_156mhz_ref_int;

// Internal 125 MHz clock
wire clk_125mhz_mmcm_out;
wire clk_125mhz_int;
wire rst_125mhz_int;

wire mmcm_rst = 1'b0;
wire mmcm_locked;
wire mmcm_clkfb;

// MMCM instance
MMCME4_BASE #(
    // 156.25 MHz input
    .CLKIN1_PERIOD(6.4),
    .REF_JITTER1(0.010),
    // 156.25 MHz input / 1 = 156.25 MHz PFD (range 10 MHz to 500 MHz)
    .DIVCLK_DIVIDE(1),
    // 156.25 MHz PFD * 8 = 1250 MHz VCO (range 800 MHz to 1600 MHz)
    .CLKFBOUT_MULT_F(8),
    .CLKFBOUT_PHASE(0),
    // 1250 MHz / 10 = 125 MHz, 0 degrees
    .CLKOUT0_DIVIDE_F(10),
    .CLKOUT0_DUTY_CYCLE(0.5),
    .CLKOUT0_PHASE(0),
    // Not used
    .CLKOUT1_DIVIDE(1),
    .CLKOUT1_DUTY_CYCLE(0.5),
    .CLKOUT1_PHASE(0),
    // Not used
    .CLKOUT2_DIVIDE(1),
    .CLKOUT2_DUTY_CYCLE(0.5),
    .CLKOUT2_PHASE(0),
    // Not used
    .CLKOUT3_DIVIDE(1),
    .CLKOUT3_DUTY_CYCLE(0.5),
    .CLKOUT3_PHASE(0),
    // Not used
    .CLKOUT4_DIVIDE(1),
    .CLKOUT4_DUTY_CYCLE(0.5),
    .CLKOUT4_PHASE(0),
    .CLKOUT4_CASCADE("FALSE"),
    // Not used
    .CLKOUT5_DIVIDE(1),
    .CLKOUT5_DUTY_CYCLE(0.5),
    .CLKOUT5_PHASE(0),
    // Not used
    .CLKOUT6_DIVIDE(1),
    .CLKOUT6_DUTY_CYCLE(0.5),
    .CLKOUT6_PHASE(0),

    // optimized bandwidth
    .BANDWIDTH("OPTIMIZED"),
    // don't wait for lock during startup
    .STARTUP_WAIT("FALSE")
)
clk_mmcm_inst (
    // 156.25 MHz input
    .CLKIN1(clk_156mhz_ref_int),
    // direct clkfb feeback
    .CLKFBIN(mmcm_clkfb),
    .CLKFBOUT(mmcm_clkfb),
    .CLKFBOUTB(),
    // 125 MHz, 0 degrees
    .CLKOUT0(clk_125mhz_mmcm_out),
    .CLKOUT0B(),
    // Not used
    .CLKOUT1(),
    .CLKOUT1B(),
    // Not used
    .CLKOUT2(),
    .CLKOUT2B(),
    // Not used
    .CLKOUT3(),
    .CLKOUT3B(),
    // Not used
    .CLKOUT4(),
    // Not used
    .CLKOUT5(),
    // Not used
    .CLKOUT6(),
    // reset input
    .RST(mmcm_rst),
    // don't power down
    .PWRDWN(1'b0),
    // locked output
    .LOCKED(mmcm_locked)
);

BUFG
clk_125mhz_bufg_inst (
    .I(clk_125mhz_mmcm_out),
    .O(clk_125mhz_int)
);

taxi_sync_reset #(
    .N(4)
)
sync_reset_125mhz_inst (
    .clk(clk_125mhz_int),
    .rst(~mmcm_locked),
    .out(rst_125mhz_int)
);

// GPIO
wire [3:0] sw_int;

taxi_debounce_switch #(
    .WIDTH(4),
    .N(4),
    .RATE(156000)
)
debounce_switch_inst (
    .clk(clk_125mhz_int),
    .rst(rst_125mhz_int),
    .in({sw}),
    .out({sw_int})
);

// SI570 I2C
wire i2c_scl_i;
wire i2c_scl_o = 1'b1;
wire i2c_scl_t = 1'b1;
wire i2c_sda_i;
wire i2c_sda_o = 1'b1;
wire i2c_sda_t = 1'b1;

assign i2c_scl_i = i2c_scl;
assign i2c_scl = i2c_scl_t ? 1'bz : i2c_scl_o;
assign i2c_sda_i = i2c_sda;
assign i2c_sda = i2c_sda_t ? 1'bz : i2c_sda_o;

localparam PORT_CNT = 2;
localparam GTY_QUAD_CNT = PORT_CNT;
localparam GTY_CNT = GTY_QUAD_CNT*4;
localparam GTY_CLK_CNT = GTY_QUAD_CNT;

wire eth_gty_tx_p[GTY_CNT];
wire eth_gty_tx_n[GTY_CNT];
wire eth_gty_rx_p[GTY_CNT];
wire eth_gty_rx_n[GTY_CNT];
wire eth_gty_mgt_refclk_p[GTY_CLK_CNT];
wire eth_gty_mgt_refclk_n[GTY_CLK_CNT];
wire eth_gty_mgt_refclk_out[GTY_CLK_CNT];

assign qsfp0_tx_p = eth_gty_tx_p[4*0 +: 4];
assign qsfp0_tx_n = eth_gty_tx_n[4*0 +: 4];
assign eth_gty_rx_p[4*0 +: 4] = qsfp0_rx_p;
assign eth_gty_rx_n[4*0 +: 4] = qsfp0_rx_n;

assign qsfp1_tx_p = eth_gty_tx_p[4*1 +: 4];
assign qsfp1_tx_n = eth_gty_tx_n[4*1 +: 4];
assign eth_gty_rx_p[4*1 +: 4] = qsfp1_rx_p;
assign eth_gty_rx_n[4*1 +: 4] = qsfp1_rx_n;

assign eth_gty_mgt_refclk_p[0] = qsfp0_mgt_refclk_0_p;
assign eth_gty_mgt_refclk_n[0] = qsfp0_mgt_refclk_0_n;
assign eth_gty_mgt_refclk_p[1] = qsfp1_mgt_refclk_0_p;
assign eth_gty_mgt_refclk_n[1] = qsfp1_mgt_refclk_0_n;

assign clk_156mhz_ref_int = eth_gty_mgt_refclk_out[0];

// Flash
wire qspi_clk_int;
wire [3:0] qspi_dq_int;
wire [3:0] qspi_dq_i_int;
wire [3:0] qspi_dq_o_int;
wire [3:0] qspi_dq_oe_int;
wire qspi_cs_int;

reg qspi_clk_reg;
reg [3:0] qspi_dq_o_reg;
reg [3:0] qspi_dq_oe_reg;
reg qspi_cs_reg;

always_ff @(posedge pcie_user_clk) begin
    qspi_clk_reg <= qspi_clk_int;
    qspi_dq_o_reg <= qspi_dq_o_int;
    qspi_dq_oe_reg <= qspi_dq_oe_int;
    qspi_cs_reg <= qspi_cs_int;
end

taxi_sync_signal #(
    .WIDTH(8),
    .N(2)
)
flash_sync_inst (
    .clk(pcie_user_clk),
    .in({qspi_dq_int}),
    .out({qspi_dq_i_int})
);

STARTUPE3
startupe3_inst (
    .CFGCLK(),
    .CFGMCLK(),
    .DI(qspi_dq_int),
    .DO(qspi_dq_o_reg),
    .DTS(~qspi_dq_oe_reg),
    .EOS(),
    .FCSBO(qspi_cs_reg),
    .FCSBTS(1'b0),
    .GSR(1'b0),
    .GTS(1'b0),
    .KEYCLEARB(1'b1),
    .PACK(1'b0),
    .PREQ(),
    .USRCCLKO(qspi_clk_reg),
    .USRCCLKTS(1'b0),
    .USRDONEO(1'b0),
    .USRDONETS(1'b1)
);

// FPGA boot
wire fpga_boot;
wire fpga_boot_sync;

taxi_sync_signal #(
    .WIDTH(1),
    .N(2)
)
fpga_boot_sync_inst (
    .clk(clk_125mhz_int),
    .in({fpga_boot}),
    .out({fpga_boot_sync})
);

wire icap_avail;
logic [2:0] icap_state_reg = 0;
logic icap_csib_reg = 1'b1;
logic icap_rdwrb_reg = 1'b0;
logic [31:0] icap_di_reg = 32'hffffffff;

wire [31:0] icap_di_rev;

assign icap_di_rev[ 7] = icap_di_reg[ 0];
assign icap_di_rev[ 6] = icap_di_reg[ 1];
assign icap_di_rev[ 5] = icap_di_reg[ 2];
assign icap_di_rev[ 4] = icap_di_reg[ 3];
assign icap_di_rev[ 3] = icap_di_reg[ 4];
assign icap_di_rev[ 2] = icap_di_reg[ 5];
assign icap_di_rev[ 1] = icap_di_reg[ 6];
assign icap_di_rev[ 0] = icap_di_reg[ 7];

assign icap_di_rev[15] = icap_di_reg[ 8];
assign icap_di_rev[14] = icap_di_reg[ 9];
assign icap_di_rev[13] = icap_di_reg[10];
assign icap_di_rev[12] = icap_di_reg[11];
assign icap_di_rev[11] = icap_di_reg[12];
assign icap_di_rev[10] = icap_di_reg[13];
assign icap_di_rev[ 9] = icap_di_reg[14];
assign icap_di_rev[ 8] = icap_di_reg[15];

assign icap_di_rev[23] = icap_di_reg[16];
assign icap_di_rev[22] = icap_di_reg[17];
assign icap_di_rev[21] = icap_di_reg[18];
assign icap_di_rev[20] = icap_di_reg[19];
assign icap_di_rev[19] = icap_di_reg[20];
assign icap_di_rev[18] = icap_di_reg[21];
assign icap_di_rev[17] = icap_di_reg[22];
assign icap_di_rev[16] = icap_di_reg[23];

assign icap_di_rev[31] = icap_di_reg[24];
assign icap_di_rev[30] = icap_di_reg[25];
assign icap_di_rev[29] = icap_di_reg[26];
assign icap_di_rev[28] = icap_di_reg[27];
assign icap_di_rev[27] = icap_di_reg[28];
assign icap_di_rev[26] = icap_di_reg[29];
assign icap_di_rev[25] = icap_di_reg[30];
assign icap_di_rev[24] = icap_di_reg[31];

always_ff @(posedge clk_125mhz_int) begin
    case (icap_state_reg)
        0: begin
            icap_state_reg <= 0;
            icap_csib_reg <= 1'b1;
            icap_rdwrb_reg <= 1'b0;
            icap_di_reg <= 32'hffffffff; // dummy word

            if (fpga_boot_sync && icap_avail) begin
                icap_state_reg <= 1;
                icap_csib_reg <= 1'b0;
                icap_rdwrb_reg <= 1'b0;
                icap_di_reg <= 32'hffffffff; // dummy word
            end
        end
        1: begin
            icap_state_reg <= 2;
            icap_csib_reg <= 1'b0;
            icap_rdwrb_reg <= 1'b0;
            icap_di_reg <= 32'hAA995566; // sync word
        end
        2: begin
            icap_state_reg <= 3;
            icap_csib_reg <= 1'b0;
            icap_rdwrb_reg <= 1'b0;
            icap_di_reg <= 32'h20000000; // type 1 noop
        end
        3: begin
            icap_state_reg <= 4;
            icap_csib_reg <= 1'b0;
            icap_rdwrb_reg <= 1'b0;
            icap_di_reg <= 32'h30008001; // write 1 word to CMD
        end
        4: begin
            icap_state_reg <= 5;
            icap_csib_reg <= 1'b0;
            icap_rdwrb_reg <= 1'b0;
            icap_di_reg <= 32'h0000000F; // IPROG
        end
        5: begin
            icap_state_reg <= 0;
            icap_csib_reg <= 1'b0;
            icap_rdwrb_reg <= 1'b0;
            icap_di_reg <= 32'h20000000; // type 1 noop
        end
    endcase
end

ICAPE3
icape3_inst (
    .AVAIL(icap_avail),
    .CLK(clk_125mhz_int),
    .CSIB(icap_csib_reg),
    .I(icap_di_rev),
    .O(),
    .PRDONE(),
    .PRERROR(),
    .RDWRB(icap_rdwrb_reg)
);

// PCIe
localparam AXIS_PCIE_DATA_W = 512;
localparam AXIS_PCIE_KEEP_W = (AXIS_PCIE_DATA_W/32);
localparam AXIS_PCIE_RC_USER_W = AXIS_PCIE_DATA_W < 512 ? 75 : 161;
localparam AXIS_PCIE_RQ_USER_W = AXIS_PCIE_DATA_W < 512 ? 62 : 137;
localparam AXIS_PCIE_CQ_USER_W = AXIS_PCIE_DATA_W < 512 ? 85 : 183;
localparam AXIS_PCIE_CC_USER_W = AXIS_PCIE_DATA_W < 512 ? 33 : 81;
localparam RC_STRADDLE = 0; // AXIS_PCIE_DATA_W >= 256;
localparam RQ_STRADDLE = 0; // AXIS_PCIE_DATA_W >= 512;
localparam CQ_STRADDLE = 0; // AXIS_PCIE_DATA_W >= 512;
localparam CC_STRADDLE = 0; // AXIS_PCIE_DATA_W >= 512;

localparam RQ_SEQ_NUM_W = AXIS_PCIE_RQ_USER_W == 60 ? 4 : 6;
localparam RQ_SEQ_NUM_EN = 1;

localparam PCIE_TAG_CNT = AXIS_PCIE_RQ_USER_W == 60 ? 64 : 256;
localparam BAR0_APERTURE = 24;

taxi_axis_if #(
    .DATA_W(AXIS_PCIE_DATA_W),
    .KEEP_EN(1),
    .KEEP_W(AXIS_PCIE_KEEP_W),
    .USER_EN(1),
    .USER_W(AXIS_PCIE_CQ_USER_W)
) axis_pcie_cq();

taxi_axis_if #(
    .DATA_W(AXIS_PCIE_DATA_W),
    .KEEP_EN(1),
    .KEEP_W(AXIS_PCIE_KEEP_W),
    .USER_EN(1),
    .USER_W(AXIS_PCIE_CC_USER_W)
) axis_pcie_cc();

taxi_axis_if #(
    .DATA_W(AXIS_PCIE_DATA_W),
    .KEEP_EN(1),
    .KEEP_W(AXIS_PCIE_KEEP_W),
    .USER_EN(1),
    .USER_W(AXIS_PCIE_RQ_USER_W)
) axis_pcie_rq();

taxi_axis_if #(
    .DATA_W(AXIS_PCIE_DATA_W),
    .KEEP_EN(1),
    .KEEP_W(AXIS_PCIE_KEEP_W),
    .USER_EN(1),
    .USER_W(AXIS_PCIE_RC_USER_W)
) axis_pcie_rc();

wire [RQ_SEQ_NUM_W-1:0] pcie_rq_seq_num0;
wire pcie_rq_seq_num_vld0;
wire [RQ_SEQ_NUM_W-1:0] pcie_rq_seq_num1;
wire pcie_rq_seq_num_vld1;

wire [2:0] cfg_max_payload;
wire [2:0] cfg_max_read_req;
wire [3:0] cfg_rcb_status;

wire [9:0]  cfg_mgmt_addr;
wire [7:0]  cfg_mgmt_function_number;
wire        cfg_mgmt_write;
wire [31:0] cfg_mgmt_write_data;
wire [3:0]  cfg_mgmt_byte_enable;
wire        cfg_mgmt_read;
wire [31:0] cfg_mgmt_read_data;
wire        cfg_mgmt_read_write_done;

wire [7:0]  cfg_fc_ph;
wire [11:0] cfg_fc_pd;
wire [7:0]  cfg_fc_nph;
wire [11:0] cfg_fc_npd;
wire [7:0]  cfg_fc_cplh;
wire [11:0] cfg_fc_cpld;
wire [2:0]  cfg_fc_sel;

wire         cfg_ext_read_received;
wire         cfg_ext_write_received;
wire [9:0]   cfg_ext_register_number;
wire [7:0]   cfg_ext_function_number;
wire [31:0]  cfg_ext_write_data;
wire [3:0]   cfg_ext_write_byte_enable;
wire [31:0]  cfg_ext_read_data;
wire         cfg_ext_read_data_valid;

wire [3:0]   cfg_flr_in_process;
wire [3:0]   cfg_flr_done;

// wire [3:0]   cfg_interrupt_msix_enable;
// wire [3:0]   cfg_interrupt_msix_mask;
// wire [251:0] cfg_interrupt_msix_vf_enable;
// wire [251:0] cfg_interrupt_msix_vf_mask;
// wire [63:0]  cfg_interrupt_msix_address;
// wire [31:0]  cfg_interrupt_msix_data;
// wire         cfg_interrupt_msix_int;
// wire [1:0]   cfg_interrupt_msix_vec_pending;
// wire         cfg_interrupt_msix_vec_pending_status;
// wire         cfg_interrupt_msix_sent;
// wire         cfg_interrupt_msix_fail;
// wire [7:0]   cfg_interrupt_msi_function_number;

wire [3:0]   cfg_interrupt_msi_enable;
wire [11:0]  cfg_interrupt_msi_mmenable;
wire         cfg_interrupt_msi_mask_update;
wire [31:0]  cfg_interrupt_msi_data;
wire [1:0]   cfg_interrupt_msi_select;
wire [31:0]  cfg_interrupt_msi_int;
wire [31:0]  cfg_interrupt_msi_pending_status;
wire         cfg_interrupt_msi_pending_status_data_enable;
wire [1:0]   cfg_interrupt_msi_pending_status_function_num;
wire         cfg_interrupt_msi_sent;
wire         cfg_interrupt_msi_fail;
wire [2:0]   cfg_interrupt_msi_attr;
wire         cfg_interrupt_msi_tph_present;
wire [1:0]   cfg_interrupt_msi_tph_type;
wire [7:0]   cfg_interrupt_msi_tph_st_tag;
wire [7:0]   cfg_interrupt_msi_function_number;

wire stat_err_cor;
wire stat_err_uncor;

wire pcie_sys_clk;
wire pcie_sys_clk_gt;

IBUFDS_GTE4 #(
    .REFCLK_HROW_CK_SEL(2'b00)
)
ibufds_gte4_pcie_refclk_inst (
    .I             (pcie_refclk_p),
    .IB            (pcie_refclk_n),
    .CEB           (1'b0),
    .O             (pcie_sys_clk_gt),
    .ODIV2         (pcie_sys_clk)
);

pcie4_uscale_plus_0
pcie4_uscale_plus_inst (
    .pci_exp_txn(pcie_tx_n),
    .pci_exp_txp(pcie_tx_p),
    .pci_exp_rxn(pcie_rx_n),
    .pci_exp_rxp(pcie_rx_p),
    .user_clk(pcie_user_clk),
    .user_reset(pcie_user_rst),
    .user_lnk_up(),

    .s_axis_rq_tdata(axis_pcie_rq.tdata),
    .s_axis_rq_tkeep(axis_pcie_rq.tkeep),
    .s_axis_rq_tlast(axis_pcie_rq.tlast),
    .s_axis_rq_tready(axis_pcie_rq.tready),
    .s_axis_rq_tuser(axis_pcie_rq.tuser),
    .s_axis_rq_tvalid(axis_pcie_rq.tvalid),

    .m_axis_rc_tdata(axis_pcie_rc.tdata),
    .m_axis_rc_tkeep(axis_pcie_rc.tkeep),
    .m_axis_rc_tlast(axis_pcie_rc.tlast),
    .m_axis_rc_tready(axis_pcie_rc.tready),
    .m_axis_rc_tuser(axis_pcie_rc.tuser),
    .m_axis_rc_tvalid(axis_pcie_rc.tvalid),

    .m_axis_cq_tdata(axis_pcie_cq.tdata),
    .m_axis_cq_tkeep(axis_pcie_cq.tkeep),
    .m_axis_cq_tlast(axis_pcie_cq.tlast),
    .m_axis_cq_tready(axis_pcie_cq.tready),
    .m_axis_cq_tuser(axis_pcie_cq.tuser),
    .m_axis_cq_tvalid(axis_pcie_cq.tvalid),

    .s_axis_cc_tdata(axis_pcie_cc.tdata),
    .s_axis_cc_tkeep(axis_pcie_cc.tkeep),
    .s_axis_cc_tlast(axis_pcie_cc.tlast),
    .s_axis_cc_tready(axis_pcie_cc.tready),
    .s_axis_cc_tuser(axis_pcie_cc.tuser),
    .s_axis_cc_tvalid(axis_pcie_cc.tvalid),

    .pcie_rq_seq_num0(pcie_rq_seq_num0),
    .pcie_rq_seq_num_vld0(pcie_rq_seq_num_vld0),
    .pcie_rq_seq_num1(pcie_rq_seq_num1),
    .pcie_rq_seq_num_vld1(pcie_rq_seq_num_vld1),
    .pcie_rq_tag0(),
    .pcie_rq_tag1(),
    .pcie_rq_tag_av(),
    .pcie_rq_tag_vld0(),
    .pcie_rq_tag_vld1(),

    .pcie_tfc_nph_av(),
    .pcie_tfc_npd_av(),

    .pcie_cq_np_req(1'b1),
    .pcie_cq_np_req_count(),

    .cfg_phy_link_down(),
    .cfg_phy_link_status(),
    .cfg_negotiated_width(),
    .cfg_current_speed(),
    .cfg_max_payload(cfg_max_payload),
    .cfg_max_read_req(cfg_max_read_req),
    .cfg_function_status(),
    .cfg_function_power_state(),
    .cfg_vf_status(),
    .cfg_vf_power_state(),
    .cfg_link_power_state(),

    .cfg_mgmt_addr(cfg_mgmt_addr),
    .cfg_mgmt_function_number(cfg_mgmt_function_number),
    .cfg_mgmt_write(cfg_mgmt_write),
    .cfg_mgmt_write_data(cfg_mgmt_write_data),
    .cfg_mgmt_byte_enable(cfg_mgmt_byte_enable),
    .cfg_mgmt_read(cfg_mgmt_read),
    .cfg_mgmt_read_data(cfg_mgmt_read_data),
    .cfg_mgmt_read_write_done(cfg_mgmt_read_write_done),
    .cfg_mgmt_debug_access(1'b0),

    .cfg_err_cor_out(),
    .cfg_err_nonfatal_out(),
    .cfg_err_fatal_out(),
    .cfg_local_error_valid(),
    .cfg_local_error_out(),
    .cfg_ltssm_state(),
    .cfg_rx_pm_state(),
    .cfg_tx_pm_state(),
    .cfg_rcb_status(cfg_rcb_status),
    .cfg_obff_enable(),
    .cfg_pl_status_change(),
    .cfg_tph_requester_enable(),
    .cfg_tph_st_mode(),
    .cfg_vf_tph_requester_enable(),
    .cfg_vf_tph_st_mode(),

    .cfg_msg_received(),
    .cfg_msg_received_data(),
    .cfg_msg_received_type(),
    .cfg_msg_transmit(1'b0),
    .cfg_msg_transmit_type(3'd0),
    .cfg_msg_transmit_data(32'd0),
    .cfg_msg_transmit_done(),

    .cfg_fc_ph(cfg_fc_ph),
    .cfg_fc_pd(cfg_fc_pd),
    .cfg_fc_nph(cfg_fc_nph),
    .cfg_fc_npd(cfg_fc_npd),
    .cfg_fc_cplh(cfg_fc_cplh),
    .cfg_fc_cpld(cfg_fc_cpld),
    .cfg_fc_sel(cfg_fc_sel),

    .cfg_dsn(64'd0),

    .cfg_bus_number(),

    .cfg_power_state_change_ack(1'b1),
    .cfg_power_state_change_interrupt(),

    .cfg_err_cor_in(stat_err_cor),
    .cfg_err_uncor_in(stat_err_uncor),
    .cfg_flr_in_process(cfg_flr_in_process),
    .cfg_flr_done(cfg_flr_done),
    .cfg_vf_flr_in_process(),
    .cfg_vf_flr_func_num(8'd0),
    .cfg_vf_flr_done(8'd0),

    .cfg_link_training_enable(1'b1),

    .cfg_ext_read_received(cfg_ext_read_received),
    .cfg_ext_write_received(cfg_ext_write_received),
    .cfg_ext_register_number(cfg_ext_register_number),
    .cfg_ext_function_number(cfg_ext_function_number),
    .cfg_ext_write_data(cfg_ext_write_data),
    .cfg_ext_write_byte_enable(cfg_ext_write_byte_enable),
    .cfg_ext_read_data(cfg_ext_read_data),
    .cfg_ext_read_data_valid(cfg_ext_read_data_valid),

    .cfg_interrupt_int(4'd0),
    .cfg_interrupt_pending(4'd0),
    .cfg_interrupt_sent(),
    // .cfg_interrupt_msix_enable(cfg_interrupt_msix_enable),
    // .cfg_interrupt_msix_mask(cfg_interrupt_msix_mask),
    // .cfg_interrupt_msix_vf_enable(cfg_interrupt_msix_vf_enable),
    // .cfg_interrupt_msix_vf_mask(cfg_interrupt_msix_vf_mask),
    // .cfg_interrupt_msix_address(cfg_interrupt_msix_address),
    // .cfg_interrupt_msix_data(cfg_interrupt_msix_data),
    // .cfg_interrupt_msix_int(cfg_interrupt_msix_int),
    // .cfg_interrupt_msix_vec_pending(cfg_interrupt_msix_vec_pending),
    // .cfg_interrupt_msix_vec_pending_status(cfg_interrupt_msix_vec_pending_status),
    // .cfg_interrupt_msi_sent(cfg_interrupt_msix_sent),
    // .cfg_interrupt_msi_fail(cfg_interrupt_msix_fail),
    // .cfg_interrupt_msi_function_number(cfg_interrupt_msi_function_number),
    .cfg_interrupt_msi_enable(cfg_interrupt_msi_enable),
    .cfg_interrupt_msi_mmenable(cfg_interrupt_msi_mmenable),
    .cfg_interrupt_msi_mask_update(cfg_interrupt_msi_mask_update),
    .cfg_interrupt_msi_data(cfg_interrupt_msi_data),
    .cfg_interrupt_msi_select(cfg_interrupt_msi_select),
    .cfg_interrupt_msi_int(cfg_interrupt_msi_int),
    .cfg_interrupt_msi_pending_status(cfg_interrupt_msi_pending_status),
    .cfg_interrupt_msi_pending_status_data_enable(cfg_interrupt_msi_pending_status_data_enable),
    .cfg_interrupt_msi_pending_status_function_num(cfg_interrupt_msi_pending_status_function_num),
    .cfg_interrupt_msi_sent(cfg_interrupt_msi_sent),
    .cfg_interrupt_msi_fail(cfg_interrupt_msi_fail),
    .cfg_interrupt_msi_attr(cfg_interrupt_msi_attr),
    .cfg_interrupt_msi_tph_present(cfg_interrupt_msi_tph_present),
    .cfg_interrupt_msi_tph_type(cfg_interrupt_msi_tph_type),
    .cfg_interrupt_msi_tph_st_tag(cfg_interrupt_msi_tph_st_tag),
    .cfg_interrupt_msi_function_number(cfg_interrupt_msi_function_number),

    .cfg_pm_aspm_l1_entry_reject(1'b0),
    .cfg_pm_aspm_tx_l0s_entry_disable(1'b0),

    .cfg_hot_reset_out(),

    .cfg_config_space_enable(1'b1),
    .cfg_req_pm_transition_l23_ready(1'b0),
    .cfg_hot_reset_in(1'b0),

    .cfg_ds_port_number(8'd0),
    .cfg_ds_bus_number(8'd0),
    .cfg_ds_device_number(5'd0),

    .sys_clk(pcie_sys_clk),
    .sys_clk_gt(pcie_sys_clk_gt),
    .sys_reset(pcie_reset_n),

    .phy_rdy_out()
);

wire uart_rxd_int[1];
wire uart_txd_int[1];

assign uart_txd = uart_txd_int[0];
assign uart_rxd_int[0] = uart_rxd;

/*
 * NAT DDR tier: DDR4 channel C2 through an AXI clock converter
 */
wire [3:0]    nat_ddr_awid;
wire [33:0]   nat_ddr_awaddr;
wire [7:0]    nat_ddr_awlen;
wire [2:0]    nat_ddr_awsize;
wire [1:0]    nat_ddr_awburst;
wire          nat_ddr_awvalid;
wire          nat_ddr_awready;
wire [511:0]  nat_ddr_wdata;
wire [63:0]   nat_ddr_wstrb;
wire          nat_ddr_wlast;
wire          nat_ddr_wvalid;
wire          nat_ddr_wready;
wire [3:0]    nat_ddr_bid;
wire [1:0]    nat_ddr_bresp;
wire          nat_ddr_bvalid;
wire          nat_ddr_bready;
wire [3:0]    nat_ddr_arid;
wire [33:0]   nat_ddr_araddr;
wire [7:0]    nat_ddr_arlen;
wire [2:0]    nat_ddr_arsize;
wire [1:0]    nat_ddr_arburst;
wire          nat_ddr_arvalid;
wire          nat_ddr_arready;
wire [3:0]    nat_ddr_rid;
wire [511:0]  nat_ddr_rdata;
wire [1:0]    nat_ddr_rresp;
wire          nat_ddr_rlast;
wire          nat_ddr_rvalid;
wire          nat_ddr_rready;
wire        nat_ddr_calib;

`ifdef NAT_DDR

wire clk_ddr4_c2_ibuf;

IBUFDS
clk_ddr4_c2_ibufds_inst (
    .I(clk_ddr4_c2_p),
    .IB(clk_ddr4_c2_n),
    .O(clk_ddr4_c2_ibuf)
);

wire ddr4_c2_ui_clk;
wire ddr4_c2_ui_rst;
wire ddr4_c2_calib_complete;
logic ddr4_c2_aresetn_reg = 1'b0;

always_ff @(posedge ddr4_c2_ui_clk) begin
    ddr4_c2_aresetn_reg <= !ddr4_c2_ui_rst;
end

logic pcie_aresetn_reg = 1'b0;

always_ff @(posedge pcie_user_clk) begin
    pcie_aresetn_reg <= !pcie_user_rst;
end

// controller side of the clock converter (ui_clk domain)
wire [3:0]   ddr_awid, ddr_arid, ddr_bid, ddr_rid;
wire [33:0]  ddr_awaddr, ddr_araddr;
wire [7:0]   ddr_awlen, ddr_arlen;
wire [2:0]   ddr_awsize, ddr_arsize;
wire [1:0]   ddr_awburst, ddr_arburst, ddr_bresp, ddr_rresp;
wire [0:0]   ddr_awlock, ddr_arlock;
wire [3:0]   ddr_awcache, ddr_arcache, ddr_awqos, ddr_arqos;
wire [2:0]   ddr_awprot, ddr_arprot;
wire         ddr_awvalid, ddr_awready, ddr_wvalid, ddr_wready, ddr_wlast;
wire         ddr_bvalid, ddr_bready, ddr_arvalid, ddr_arready;
wire         ddr_rvalid, ddr_rready, ddr_rlast;
wire [511:0] ddr_wdata, ddr_rdata;
wire [63:0]  ddr_wstrb;
wire [7:0]   ddr_bid_full, ddr_rid_full;

assign ddr_bid = ddr_bid_full[3:0];
assign ddr_rid = ddr_rid_full[3:0];

axi_cc_natgw_ddr
ddr_cc_inst (
    .s_axi_aclk(pcie_user_clk),
    .s_axi_aresetn(pcie_aresetn_reg),
    .s_axi_awid(nat_ddr_awid),
    .s_axi_awaddr(nat_ddr_awaddr),
    .s_axi_awlen(nat_ddr_awlen),
    .s_axi_awsize(nat_ddr_awsize),
    .s_axi_awburst(nat_ddr_awburst),
    .s_axi_awlock(1'b0),
    .s_axi_awcache(4'b0011),
    .s_axi_awprot(3'b010),
    .s_axi_awregion(4'd0),
    .s_axi_awqos(4'd0),
    .s_axi_awvalid(nat_ddr_awvalid),
    .s_axi_awready(nat_ddr_awready),
    .s_axi_wdata(nat_ddr_wdata),
    .s_axi_wstrb(nat_ddr_wstrb),
    .s_axi_wlast(nat_ddr_wlast),
    .s_axi_wvalid(nat_ddr_wvalid),
    .s_axi_wready(nat_ddr_wready),
    .s_axi_bid(nat_ddr_bid),
    .s_axi_bresp(nat_ddr_bresp),
    .s_axi_bvalid(nat_ddr_bvalid),
    .s_axi_bready(nat_ddr_bready),
    .s_axi_arid(nat_ddr_arid),
    .s_axi_araddr(nat_ddr_araddr),
    .s_axi_arlen(nat_ddr_arlen),
    .s_axi_arsize(nat_ddr_arsize),
    .s_axi_arburst(nat_ddr_arburst),
    .s_axi_arlock(1'b0),
    .s_axi_arcache(4'b0011),
    .s_axi_arprot(3'b010),
    .s_axi_arregion(4'd0),
    .s_axi_arqos(4'd0),
    .s_axi_arvalid(nat_ddr_arvalid),
    .s_axi_arready(nat_ddr_arready),
    .s_axi_rid(nat_ddr_rid),
    .s_axi_rdata(nat_ddr_rdata),
    .s_axi_rresp(nat_ddr_rresp),
    .s_axi_rlast(nat_ddr_rlast),
    .s_axi_rvalid(nat_ddr_rvalid),
    .s_axi_rready(nat_ddr_rready),

    .m_axi_aclk(ddr4_c2_ui_clk),
    .m_axi_aresetn(ddr4_c2_aresetn_reg),
    .m_axi_awid(ddr_awid),
    .m_axi_awaddr(ddr_awaddr),
    .m_axi_awlen(ddr_awlen),
    .m_axi_awsize(ddr_awsize),
    .m_axi_awburst(ddr_awburst),
    .m_axi_awlock(ddr_awlock),
    .m_axi_awcache(ddr_awcache),
    .m_axi_awprot(ddr_awprot),
    .m_axi_awregion(),
    .m_axi_awqos(ddr_awqos),
    .m_axi_awvalid(ddr_awvalid),
    .m_axi_awready(ddr_awready),
    .m_axi_wdata(ddr_wdata),
    .m_axi_wstrb(ddr_wstrb),
    .m_axi_wlast(ddr_wlast),
    .m_axi_wvalid(ddr_wvalid),
    .m_axi_wready(ddr_wready),
    .m_axi_bid(ddr_bid),
    .m_axi_bresp(ddr_bresp),
    .m_axi_bvalid(ddr_bvalid),
    .m_axi_bready(ddr_bready),
    .m_axi_arid(ddr_arid),
    .m_axi_araddr(ddr_araddr),
    .m_axi_arlen(ddr_arlen),
    .m_axi_arsize(ddr_arsize),
    .m_axi_arburst(ddr_arburst),
    .m_axi_arlock(ddr_arlock),
    .m_axi_arcache(ddr_arcache),
    .m_axi_arprot(ddr_arprot),
    .m_axi_arregion(),
    .m_axi_arqos(ddr_arqos),
    .m_axi_arvalid(ddr_arvalid),
    .m_axi_arready(ddr_arready),
    .m_axi_rid(ddr_rid),
    .m_axi_rdata(ddr_rdata),
    .m_axi_rresp(ddr_rresp),
    .m_axi_rlast(ddr_rlast),
    .m_axi_rvalid(ddr_rvalid),
    .m_axi_rready(ddr_rready)
);

ddr4_0
ddr4_c2_inst (
    .sys_rst(rst_125mhz_int),
    .c0_sys_clk_i(clk_ddr4_c2_ibuf),
    .c0_init_calib_complete(ddr4_c2_calib_complete),
    .dbg_clk(),
    .dbg_bus(),

    .c0_ddr4_adr(ddr4_c2_adr),
    .c0_ddr4_ba(ddr4_c2_ba),
    .c0_ddr4_cke(ddr4_c2_cke),
    .c0_ddr4_cs_n(ddr4_c2_cs_n),
    .c0_ddr4_dq(ddr4_c2_dq),
    .c0_ddr4_dqs_c(ddr4_c2_dqs_c),
    .c0_ddr4_dqs_t(ddr4_c2_dqs_t),
    .c0_ddr4_odt(ddr4_c2_odt),
    .c0_ddr4_parity(ddr4_c2_par),
    .c0_ddr4_bg(ddr4_c2_bg),
    .c0_ddr4_reset_n(ddr4_c2_reset_n),
    .c0_ddr4_act_n(ddr4_c2_act_n),
    .c0_ddr4_ck_c(ddr4_c2_ck_c),
    .c0_ddr4_ck_t(ddr4_c2_ck_t),

    .c0_ddr4_ui_clk(ddr4_c2_ui_clk),
    .c0_ddr4_ui_clk_sync_rst(ddr4_c2_ui_rst),
    .c0_ddr4_aresetn(ddr4_c2_aresetn_reg),

    // ECC control interface: unused (ECC on, defaults)
    .c0_ddr4_s_axi_ctrl_awvalid(1'b0),
    .c0_ddr4_s_axi_ctrl_awready(),
    .c0_ddr4_s_axi_ctrl_awaddr(32'd0),
    .c0_ddr4_s_axi_ctrl_wvalid(1'b0),
    .c0_ddr4_s_axi_ctrl_wready(),
    .c0_ddr4_s_axi_ctrl_wdata(32'd0),
    .c0_ddr4_s_axi_ctrl_bvalid(),
    .c0_ddr4_s_axi_ctrl_bready(1'b1),
    .c0_ddr4_s_axi_ctrl_bresp(),
    .c0_ddr4_s_axi_ctrl_arvalid(1'b0),
    .c0_ddr4_s_axi_ctrl_arready(),
    .c0_ddr4_s_axi_ctrl_araddr(32'd0),
    .c0_ddr4_s_axi_ctrl_rvalid(),
    .c0_ddr4_s_axi_ctrl_rready(1'b1),
    .c0_ddr4_s_axi_ctrl_rdata(),
    .c0_ddr4_s_axi_ctrl_rresp(),
    .c0_ddr4_interrupt(),

    .c0_ddr4_s_axi_awid({4'd0, ddr_awid}),
    .c0_ddr4_s_axi_awaddr(ddr_awaddr),
    .c0_ddr4_s_axi_awlen(ddr_awlen),
    .c0_ddr4_s_axi_awsize(ddr_awsize),
    .c0_ddr4_s_axi_awburst(ddr_awburst),
    .c0_ddr4_s_axi_awlock(ddr_awlock),
    .c0_ddr4_s_axi_awcache(ddr_awcache),
    .c0_ddr4_s_axi_awprot(ddr_awprot),
    .c0_ddr4_s_axi_awqos(ddr_awqos),
    .c0_ddr4_s_axi_awvalid(ddr_awvalid),
    .c0_ddr4_s_axi_awready(ddr_awready),
    .c0_ddr4_s_axi_wdata(ddr_wdata),
    .c0_ddr4_s_axi_wstrb(ddr_wstrb),
    .c0_ddr4_s_axi_wlast(ddr_wlast),
    .c0_ddr4_s_axi_wvalid(ddr_wvalid),
    .c0_ddr4_s_axi_wready(ddr_wready),
    .c0_ddr4_s_axi_bready(ddr_bready),
    .c0_ddr4_s_axi_bid(ddr_bid_full),
    .c0_ddr4_s_axi_bresp(ddr_bresp),
    .c0_ddr4_s_axi_bvalid(ddr_bvalid),
    .c0_ddr4_s_axi_arid({4'd0, ddr_arid}),
    .c0_ddr4_s_axi_araddr(ddr_araddr),
    .c0_ddr4_s_axi_arlen(ddr_arlen),
    .c0_ddr4_s_axi_arsize(ddr_arsize),
    .c0_ddr4_s_axi_arburst(ddr_arburst),
    .c0_ddr4_s_axi_arlock(ddr_arlock),
    .c0_ddr4_s_axi_arcache(ddr_arcache),
    .c0_ddr4_s_axi_arprot(ddr_arprot),
    .c0_ddr4_s_axi_arqos(ddr_arqos),
    .c0_ddr4_s_axi_arvalid(ddr_arvalid),
    .c0_ddr4_s_axi_arready(ddr_arready),
    .c0_ddr4_s_axi_rready(ddr_rready),
    .c0_ddr4_s_axi_rlast(ddr_rlast),
    .c0_ddr4_s_axi_rvalid(ddr_rvalid),
    .c0_ddr4_s_axi_rresp(ddr_rresp),
    .c0_ddr4_s_axi_rid(ddr_rid_full),
    .c0_ddr4_s_axi_rdata(ddr_rdata)
);

// calibration status: no DIMM (or a failed one) leaves it low, and the
// host then never places flows in DDR
taxi_sync_signal #(
    .WIDTH(1),
    .N(3)
)
ddr4_c2_calib_sync_inst (
    .clk(pcie_user_clk),
    .in(ddr4_c2_calib_complete),
    .out(nat_ddr_calib)
);

`else

assign nat_ddr_awready = '0;
assign nat_ddr_wready = '0;
assign nat_ddr_bid = '0;
assign nat_ddr_bresp = '0;
assign nat_ddr_bvalid = '0;
assign nat_ddr_arready = '0;
assign nat_ddr_rid = '0;
assign nat_ddr_rdata = '0;
assign nat_ddr_rresp = '0;
assign nat_ddr_rlast = '0;
assign nat_ddr_rvalid = '0;
assign nat_ddr_calib = 1'b0;

`endif

fpga_core_nat #(
    .SIM(SIM),
    .VENDOR(VENDOR),
    .FAMILY(FAMILY),

    // Board configuration
    .SW_CNT(4),
    .LED_CNT(3),
    .UART_CNT(1),
    .PORT_CNT(PORT_CNT),
    .GTY_QUAD_CNT(GTY_QUAD_CNT),
    .GTY_CNT(GTY_CNT),
    .GTY_CLK_CNT(GTY_CLK_CNT),

    // FW ID
    .FPGA_ID(FPGA_ID),
    .FW_ID(FW_ID),
    .FW_VER(FW_VER),
    .BOARD_ID(BOARD_ID),
    .BOARD_VER(BOARD_VER),
    .BUILD_DATE(BUILD_DATE),
    .GIT_HASH(GIT_HASH),
    .RELEASE_INFO(RELEASE_INFO),

    // PTP configuration
    .PTP_TS_EN(PTP_TS_EN),
    .PTP_CLK_PER_NS_NUM(32),
    .PTP_CLK_PER_NS_DEN(5),

    // PCIe interface configuration
    .RQ_SEQ_NUM_W(RQ_SEQ_NUM_W),

    // AXI lite interface configuration (control)
    .AXIL_CTRL_DATA_W(AXIL_CTRL_DATA_W),
    .AXIL_CTRL_ADDR_W(AXIL_CTRL_ADDR_W),

    // MAC configuration
    .CFG_LOW_LATENCY(CFG_LOW_LATENCY),
    .COMBINED_MAC_PCS(COMBINED_MAC_PCS),
    .MAC_DATA_W(MAC_DATA_W),

    // NAT shim
    .NAT_BUCKET_W(NAT_BUCKET_W),
    .NAT_RAM_PIPE(NAT_RAM_PIPE),
    .NAT_TICK_DIV(250000),
`ifdef NAT_DDR
    .NAT_DDR(1),
`else
    .NAT_DDR(0),
`endif
    .NAT_DDR_BUCKET_W(NAT_DDR_BUCKET_W),
    .NAT_DDR_AXI_ADDR_W(34),
    .NAT_DDR_AXI_ID_W(4)
)
core_inst (
    /*
     * Clock: 125MHz
     * Synchronous reset
     */
    .clk_125mhz(clk_125mhz_int),
    .rst_125mhz(rst_125mhz_int),

    /*
     * GPIO
     */
    .sw(sw_int),
    .led(led),
    .port_led_act(),
    .port_led_stat_r(),
    .port_led_stat_g(),
    .port_led_stat_b(),
    .port_led_stat_y(),

    /*
     * UART
     */
    .uart_txd(uart_txd_int),
    .uart_rxd(uart_rxd_int),

    /*
     * Ethernet
     */
    .eth_gty_tx_p(eth_gty_tx_p),
    .eth_gty_tx_n(eth_gty_tx_n),
    .eth_gty_rx_p(eth_gty_rx_p),
    .eth_gty_rx_n(eth_gty_rx_n),
    .eth_gty_mgt_refclk_p(eth_gty_mgt_refclk_p),
    .eth_gty_mgt_refclk_n(eth_gty_mgt_refclk_n),
    .eth_gty_mgt_refclk_out(eth_gty_mgt_refclk_out),

    .eth_port_modsell({qsfp1_modsell, qsfp0_modsell}),
    .eth_port_resetl({qsfp1_resetl, qsfp0_resetl}),
    .eth_port_modprsl({qsfp1_modprsl, qsfp0_modprsl}),
    .eth_port_intl({qsfp1_intl, qsfp0_intl}),
    .eth_port_lpmode({qsfp1_lpmode, qsfp0_lpmode}),

    /*
     * PCIe
     */
    .pcie_clk(pcie_user_clk),
    .pcie_rst(pcie_user_rst),
    .s_axis_pcie_cq(axis_pcie_cq),
    .m_axis_pcie_cc(axis_pcie_cc),
    .m_axis_pcie_rq(axis_pcie_rq),
    .s_axis_pcie_rc(axis_pcie_rc),

    .pcie_rq_seq_num0(pcie_rq_seq_num0),
    .pcie_rq_seq_num_vld0(pcie_rq_seq_num_vld0),
    .pcie_rq_seq_num1(pcie_rq_seq_num1),
    .pcie_rq_seq_num_vld1(pcie_rq_seq_num_vld1),

    .cfg_max_payload(cfg_max_payload),
    .cfg_max_read_req(cfg_max_read_req),
    .cfg_rcb_status(cfg_rcb_status),

    .cfg_mgmt_addr(cfg_mgmt_addr),
    .cfg_mgmt_function_number(cfg_mgmt_function_number),
    .cfg_mgmt_write(cfg_mgmt_write),
    .cfg_mgmt_write_data(cfg_mgmt_write_data),
    .cfg_mgmt_byte_enable(cfg_mgmt_byte_enable),
    .cfg_mgmt_read(cfg_mgmt_read),
    .cfg_mgmt_read_data(cfg_mgmt_read_data),
    .cfg_mgmt_read_write_done(cfg_mgmt_read_write_done),

    .cfg_fc_ph(cfg_fc_ph),
    .cfg_fc_pd(cfg_fc_pd),
    .cfg_fc_nph(cfg_fc_nph),
    .cfg_fc_npd(cfg_fc_npd),
    .cfg_fc_cplh(cfg_fc_cplh),
    .cfg_fc_cpld(cfg_fc_cpld),
    .cfg_fc_sel(cfg_fc_sel),

    .cfg_ext_read_received(cfg_ext_read_received),
    .cfg_ext_write_received(cfg_ext_write_received),
    .cfg_ext_register_number(cfg_ext_register_number),
    .cfg_ext_function_number(cfg_ext_function_number),
    .cfg_ext_write_data(cfg_ext_write_data),
    .cfg_ext_write_byte_enable(cfg_ext_write_byte_enable),
    .cfg_ext_read_data(cfg_ext_read_data),
    .cfg_ext_read_data_valid(cfg_ext_read_data_valid),

    // .cfg_interrupt_msix_enable(cfg_interrupt_msix_enable),
    // .cfg_interrupt_msix_mask(cfg_interrupt_msix_mask),
    // .cfg_interrupt_msix_vf_enable(cfg_interrupt_msix_vf_enable),
    // .cfg_interrupt_msix_vf_mask(cfg_interrupt_msix_vf_mask),
    // .cfg_interrupt_msix_address(cfg_interrupt_msix_address),
    // .cfg_interrupt_msix_data(cfg_interrupt_msix_data),
    // .cfg_interrupt_msix_int(cfg_interrupt_msix_int),
    // .cfg_interrupt_msix_vec_pending(cfg_interrupt_msix_vec_pending),
    // .cfg_interrupt_msix_vec_pending_status(cfg_interrupt_msix_vec_pending_status),
    // .cfg_interrupt_msix_sent(cfg_interrupt_msix_sent),
    // .cfg_interrupt_msix_fail(cfg_interrupt_msix_fail),
    // .cfg_interrupt_msi_function_number(cfg_interrupt_msi_function_number),

    .cfg_interrupt_msi_enable(cfg_interrupt_msi_enable),
    .cfg_interrupt_msi_mmenable(cfg_interrupt_msi_mmenable),
    .cfg_interrupt_msi_mask_update(cfg_interrupt_msi_mask_update),
    .cfg_interrupt_msi_data(cfg_interrupt_msi_data),
    .cfg_interrupt_msi_select(cfg_interrupt_msi_select),
    .cfg_interrupt_msi_int(cfg_interrupt_msi_int),
    .cfg_interrupt_msi_pending_status(cfg_interrupt_msi_pending_status),
    .cfg_interrupt_msi_pending_status_data_enable(cfg_interrupt_msi_pending_status_data_enable),
    .cfg_interrupt_msi_pending_status_function_num(cfg_interrupt_msi_pending_status_function_num),
    .cfg_interrupt_msi_sent(cfg_interrupt_msi_sent),
    .cfg_interrupt_msi_fail(cfg_interrupt_msi_fail),
    .cfg_interrupt_msi_attr(cfg_interrupt_msi_attr),
    .cfg_interrupt_msi_tph_present(cfg_interrupt_msi_tph_present),
    .cfg_interrupt_msi_tph_type(cfg_interrupt_msi_tph_type),
    .cfg_interrupt_msi_tph_st_tag(cfg_interrupt_msi_tph_st_tag),
    .cfg_interrupt_msi_function_number(cfg_interrupt_msi_function_number),

    .cfg_flr_in_process(cfg_flr_in_process),
    .cfg_flr_done(cfg_flr_done),

    /*
     * QSPI flash
     */
    .fpga_boot(fpga_boot),
    .qspi_clk(qspi_clk_int),
    .qspi_dq_i(qspi_dq_i_int),
    .qspi_dq_o(qspi_dq_o_int),
    .qspi_dq_oe(qspi_dq_oe_int),
    .qspi_cs(qspi_cs_int),

    /*
     * NAT DDR tier
     */
    .m_axi_ddr_awid(nat_ddr_awid),
    .m_axi_ddr_awaddr(nat_ddr_awaddr),
    .m_axi_ddr_awlen(nat_ddr_awlen),
    .m_axi_ddr_awsize(nat_ddr_awsize),
    .m_axi_ddr_awburst(nat_ddr_awburst),
    .m_axi_ddr_awvalid(nat_ddr_awvalid),
    .m_axi_ddr_awready(nat_ddr_awready),
    .m_axi_ddr_wdata(nat_ddr_wdata),
    .m_axi_ddr_wstrb(nat_ddr_wstrb),
    .m_axi_ddr_wlast(nat_ddr_wlast),
    .m_axi_ddr_wvalid(nat_ddr_wvalid),
    .m_axi_ddr_wready(nat_ddr_wready),
    .m_axi_ddr_bid(nat_ddr_bid),
    .m_axi_ddr_bresp(nat_ddr_bresp),
    .m_axi_ddr_bvalid(nat_ddr_bvalid),
    .m_axi_ddr_bready(nat_ddr_bready),
    .m_axi_ddr_arid(nat_ddr_arid),
    .m_axi_ddr_araddr(nat_ddr_araddr),
    .m_axi_ddr_arlen(nat_ddr_arlen),
    .m_axi_ddr_arsize(nat_ddr_arsize),
    .m_axi_ddr_arburst(nat_ddr_arburst),
    .m_axi_ddr_arvalid(nat_ddr_arvalid),
    .m_axi_ddr_arready(nat_ddr_arready),
    .m_axi_ddr_rid(nat_ddr_rid),
    .m_axi_ddr_rdata(nat_ddr_rdata),
    .m_axi_ddr_rresp(nat_ddr_rresp),
    .m_axi_ddr_rlast(nat_ddr_rlast),
    .m_axi_ddr_rvalid(nat_ddr_rvalid),
    .m_axi_ddr_rready(nat_ddr_rready),
    .ddr_calib(nat_ddr_calib)
);

endmodule

`resetall
