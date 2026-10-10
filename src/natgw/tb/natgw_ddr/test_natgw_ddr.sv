// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: DDR tier testbench (the AXI memory is a cocotb model)

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_ddr
    import natgw_pkg::*;
#(
    /* verilator lint_off WIDTHTRUNC */
    parameter DDR_BUCKET_W = 6,
    parameter MAX_OUT = 4,
    parameter QUEUE_DEPTH = 64,
    parameter ACT_PIPE = 2
    /* verilator lint_on WIDTHTRUNC */
)
();

localparam AXI_ADDR_W = 16;
localparam AXI_ID_W = 4;
localparam DIDX_W = DDR_BUCKET_W + 2;

logic clk;
logic rst;

logic s_valid;
logic [LANE_W-1:0] s_lane;
result_t s_res;
key_t s_key;
logic [31:0] s_h1;
logic s_lookup;

logic m_valid;
logic [LANE_W-1:0] m_lane;
result_t m_res;

logic [AXI_ID_W-1:0] m_axi_awid;
logic [AXI_ADDR_W-1:0] m_axi_awaddr;
logic [7:0] m_axi_awlen;
logic [2:0] m_axi_awsize;
logic [1:0] m_axi_awburst;
logic m_axi_awvalid;
logic m_axi_awready;
logic [511:0] m_axi_wdata;
logic [63:0] m_axi_wstrb;
logic m_axi_wlast;
logic m_axi_wvalid;
logic m_axi_wready;
logic [AXI_ID_W-1:0] m_axi_bid;
logic [1:0] m_axi_bresp;
logic m_axi_bvalid;
logic m_axi_bready;
logic [AXI_ID_W-1:0] m_axi_arid;
logic [AXI_ADDR_W-1:0] m_axi_araddr;
logic [7:0] m_axi_arlen;
logic [2:0] m_axi_arsize;
logic [1:0] m_axi_arburst;
logic m_axi_arvalid;
logic m_axi_arready;
logic [AXI_ID_W-1:0] m_axi_rid;
logic [511:0] m_axi_rdata;
logic [1:0] m_axi_rresp;
logic m_axi_rlast;
logic m_axi_rvalid;
logic m_axi_rready;

logic ddr_calib;
logic cfg_ddr_en;
logic ddr_active;
logic clear_start;
logic clear_busy;

logic host_valid;
logic host_ready;
logic [1:0] host_op;
logic [DIDX_W-1:0] host_idx;
entry_t host_wdata;
logic host_done;
entry_t host_rdata;

logic nh_wr_valid;
logic [NH_IDX_W-1:0] nh_wr_idx;
nh_t nh_wr_data;
logic nh_clear_start;

logic act_valid;
logic act_ready;
logic [DIDX_W-7:0] act_word;
logic act_rvalid;
logic [63:0] act_rdata;

logic stat_lookup;
logic stat_hit;
logic stat_skip;
logic stat_rerr;
logic err_overflow;

// test: force an error response (SLVERR) on the read data channel
logic rresp_err;

natgw_ddr #(
    .DDR_BUCKET_W(DDR_BUCKET_W),
    .AXI_ADDR_W(AXI_ADDR_W),
    .AXI_ID_W(AXI_ID_W),
    .MAX_OUT(MAX_OUT),
    .QUEUE_DEPTH(QUEUE_DEPTH),
    .ACT_PIPE(ACT_PIPE)
)
uut (
    .m_axi_rresp(m_axi_rresp | {rresp_err, 1'b0}),
    .*
);

endmodule

`resetall
