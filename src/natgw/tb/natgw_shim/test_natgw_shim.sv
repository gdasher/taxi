// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim testbench

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_shim #
(
    /* verilator lint_off WIDTHTRUNC */
    parameter BUCKET_W = 8,
    parameter RAM_PIPE = 3,
    parameter TICK_DIV_RST = 64,
    parameter HOLD_DEPTH = 16384,
    parameter DESC_DEPTH = 32,
    parameter CDC_DEPTH = 16384,
    parameter PTP_TS_W = 48,
    parameter DDR_ENABLE = 0,
    parameter DDR_BUCKET_W = 8
    /* verilator lint_on WIDTHTRUNC */
)
();

logic clk;
logic rst;

logic mac_rx_clk_0, mac_rx_rst_0, mac_tx_clk_0, mac_tx_rst_0;
logic mac_rx_clk_1, mac_rx_rst_1, mac_tx_clk_1, mac_tx_rst_1;
logic mac_rx_clk_2, mac_rx_rst_2, mac_tx_clk_2, mac_tx_rst_2;
logic mac_rx_clk_3, mac_rx_rst_3, mac_tx_clk_3, mac_tx_rst_3;
logic mac_rx_clk_4, mac_rx_rst_4, mac_tx_clk_4, mac_tx_rst_4;
logic mac_rx_clk_5, mac_rx_rst_5, mac_tx_clk_5, mac_tx_rst_5;
logic mac_rx_clk_6, mac_rx_rst_6, mac_tx_clk_6, mac_tx_rst_6;
logic mac_rx_clk_7, mac_rx_rst_7, mac_tx_clk_7, mac_tx_rst_7;

wire mac_rx_clk[8];
wire mac_rx_rst[8];
wire mac_tx_clk[8];
wire mac_tx_rst[8];

assign mac_rx_clk[0] = mac_rx_clk_0;
assign mac_rx_rst[0] = mac_rx_rst_0;
assign mac_tx_clk[0] = mac_tx_clk_0;
assign mac_tx_rst[0] = mac_tx_rst_0;
assign mac_rx_clk[1] = mac_rx_clk_1;
assign mac_rx_rst[1] = mac_rx_rst_1;
assign mac_tx_clk[1] = mac_tx_clk_1;
assign mac_tx_rst[1] = mac_tx_rst_1;
assign mac_rx_clk[2] = mac_rx_clk_2;
assign mac_rx_rst[2] = mac_rx_rst_2;
assign mac_tx_clk[2] = mac_tx_clk_2;
assign mac_tx_rst[2] = mac_tx_rst_2;
assign mac_rx_clk[3] = mac_rx_clk_3;
assign mac_rx_rst[3] = mac_rx_rst_3;
assign mac_tx_clk[3] = mac_tx_clk_3;
assign mac_tx_rst[3] = mac_tx_rst_3;
assign mac_rx_clk[4] = mac_rx_clk_4;
assign mac_rx_rst[4] = mac_rx_rst_4;
assign mac_tx_clk[4] = mac_tx_clk_4;
assign mac_tx_rst[4] = mac_tx_rst_4;
assign mac_rx_clk[5] = mac_rx_clk_5;
assign mac_rx_rst[5] = mac_rx_rst_5;
assign mac_tx_clk[5] = mac_tx_clk_5;
assign mac_tx_rst[5] = mac_tx_rst_5;
assign mac_rx_clk[6] = mac_rx_clk_6;
assign mac_rx_rst[6] = mac_rx_rst_6;
assign mac_tx_clk[6] = mac_tx_clk_6;
assign mac_tx_rst[6] = mac_tx_rst_6;
assign mac_rx_clk[7] = mac_rx_clk_7;
assign mac_rx_rst[7] = mac_rx_rst_7;
assign mac_tx_clk[7] = mac_tx_clk_7;
assign mac_tx_rst[7] = mac_tx_rst_7;

taxi_axil_if #(.DATA_W(32), .ADDR_W(16)) s_axil();

taxi_axis_if #(.DATA_W(64), .USER_EN(1), .USER_W(1+PTP_TS_W)) mac_rx[8]();
taxi_axis_if #(.DATA_W(64), .ID_EN(1), .ID_W(8), .USER_EN(1), .USER_W(1)) mac_tx[8]();
taxi_axis_if #(.DATA_W(PTP_TS_W), .KEEP_W(1), .ID_EN(1), .ID_W(8)) mac_tx_cpl[8]();
taxi_axis_if #(.DATA_W(64), .USER_EN(1), .USER_W(1)) core_tx[8]();
taxi_axis_if #(.DATA_W(PTP_TS_W), .KEEP_W(1), .ID_EN(1), .ID_W(8)) core_tx_cpl[8]();
taxi_axis_if #(.DATA_W(64), .USER_EN(1), .USER_W(1+PTP_TS_W)) core_rx[8]();

logic clear_busy;

// DDR tier (an AXI memory model in the testbench)
localparam DDR_AXI_ADDR_W = 24;
localparam DDR_AXI_ID_W = 4;
logic [DDR_AXI_ID_W-1:0] m_axi_ddr_awid;
logic [DDR_AXI_ADDR_W-1:0] m_axi_ddr_awaddr;
logic [7:0] m_axi_ddr_awlen;
logic [2:0] m_axi_ddr_awsize;
logic [1:0] m_axi_ddr_awburst;
logic m_axi_ddr_awvalid;
logic m_axi_ddr_awready;
logic [511:0] m_axi_ddr_wdata;
logic [63:0] m_axi_ddr_wstrb;
logic m_axi_ddr_wlast;
logic m_axi_ddr_wvalid;
logic m_axi_ddr_wready;
logic [DDR_AXI_ID_W-1:0] m_axi_ddr_bid;
logic [1:0] m_axi_ddr_bresp;
logic m_axi_ddr_bvalid;
logic m_axi_ddr_bready;
logic [DDR_AXI_ID_W-1:0] m_axi_ddr_arid;
logic [DDR_AXI_ADDR_W-1:0] m_axi_ddr_araddr;
logic [7:0] m_axi_ddr_arlen;
logic [2:0] m_axi_ddr_arsize;
logic [1:0] m_axi_ddr_arburst;
logic m_axi_ddr_arvalid;
logic m_axi_ddr_arready;
logic [DDR_AXI_ID_W-1:0] m_axi_ddr_rid;
logic [511:0] m_axi_ddr_rdata;
logic [1:0] m_axi_ddr_rresp;
logic m_axi_ddr_rlast;
logic m_axi_ddr_rvalid;
logic m_axi_ddr_rready;
logic ddr_calib;

natgw_shim #(
    .BUCKET_W(BUCKET_W),
    .RAM_PIPE(RAM_PIPE),
    .TICK_DIV_RST(TICK_DIV_RST),
    .HOLD_DEPTH(HOLD_DEPTH),
    .DESC_DEPTH(DESC_DEPTH),
    .CDC_DEPTH(CDC_DEPTH),
    .DDR_ENABLE(DDR_ENABLE),
    .DDR_BUCKET_W(DDR_BUCKET_W),
    .DDR_AXI_ADDR_W(DDR_AXI_ADDR_W),
    .DDR_AXI_ID_W(DDR_AXI_ID_W)
)
uut (
    .clk(clk),
    .rst(rst),
    .s_axil_wr(s_axil),
    .s_axil_rd(s_axil),
    .mac_tx_clk(mac_tx_clk),
    .mac_tx_rst(mac_tx_rst),
    .m_axis_mac_tx(mac_tx),
    .s_axis_mac_tx_cpl(mac_tx_cpl),
    .mac_rx_clk(mac_rx_clk),
    .mac_rx_rst(mac_rx_rst),
    .s_axis_mac_rx(mac_rx),
    .s_axis_core_tx(core_tx),
    .m_axis_core_tx_cpl(core_tx_cpl),
    .m_axis_core_rx(core_rx),
    .m_axi_ddr_awid(m_axi_ddr_awid),
    .m_axi_ddr_awaddr(m_axi_ddr_awaddr),
    .m_axi_ddr_awlen(m_axi_ddr_awlen),
    .m_axi_ddr_awsize(m_axi_ddr_awsize),
    .m_axi_ddr_awburst(m_axi_ddr_awburst),
    .m_axi_ddr_awvalid(m_axi_ddr_awvalid),
    .m_axi_ddr_awready(m_axi_ddr_awready),
    .m_axi_ddr_wdata(m_axi_ddr_wdata),
    .m_axi_ddr_wstrb(m_axi_ddr_wstrb),
    .m_axi_ddr_wlast(m_axi_ddr_wlast),
    .m_axi_ddr_wvalid(m_axi_ddr_wvalid),
    .m_axi_ddr_wready(m_axi_ddr_wready),
    .m_axi_ddr_bid(m_axi_ddr_bid),
    .m_axi_ddr_bresp(m_axi_ddr_bresp),
    .m_axi_ddr_bvalid(m_axi_ddr_bvalid),
    .m_axi_ddr_bready(m_axi_ddr_bready),
    .m_axi_ddr_arid(m_axi_ddr_arid),
    .m_axi_ddr_araddr(m_axi_ddr_araddr),
    .m_axi_ddr_arlen(m_axi_ddr_arlen),
    .m_axi_ddr_arsize(m_axi_ddr_arsize),
    .m_axi_ddr_arburst(m_axi_ddr_arburst),
    .m_axi_ddr_arvalid(m_axi_ddr_arvalid),
    .m_axi_ddr_arready(m_axi_ddr_arready),
    .m_axi_ddr_rid(m_axi_ddr_rid),
    .m_axi_ddr_rdata(m_axi_ddr_rdata),
    .m_axi_ddr_rresp(m_axi_ddr_rresp),
    .m_axi_ddr_rlast(m_axi_ddr_rlast),
    .m_axi_ddr_rvalid(m_axi_ddr_rvalid),
    .m_axi_ddr_rready(m_axi_ddr_rready),
    .ddr_calib(ddr_calib),
    .clear_busy(clear_busy)
);

endmodule

`resetall
