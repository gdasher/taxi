// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: per-entry state testbench

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_state #
(
    /* verilator lint_off WIDTHTRUNC */
    parameter IDX_W = 8,
    parameter RAM_PIPE = 3,
    parameter EVT_DEPTH = 32
    /* verilator lint_on WIDTHTRUNC */
)
();

localparam STATE_W = natgw_pkg::STATE_W;

logic clk;
logic rst;

logic s_hit_valid;
logic [IDX_W-1:0] s_hit_idx;
logic [15:0] s_hit_len;
logic s_hit_fin;
logic s_hit_rst;
logic bubble_req;

logic host_st_valid;
logic host_st_ready;
logic host_st_we;
logic [IDX_W-1:0] host_st_idx;
logic [STATE_W-1:0] host_st_wdata;
logic host_st_rvalid;
logic [STATE_W-1:0] host_st_rdata;

logic m_evt_valid;
logic m_evt_ready;
logic [63:0] m_evt;

logic [31:0] cfg_tick;
logic [31:0] cfg_thresh_tcp;
logic [31:0] cfg_thresh_udp;
logic cfg_scan_en;
logic [15:0] cfg_scan_interval;

logic clear_start;
logic clear_busy;
logic stat_evt_drop;

natgw_state #(
    .IDX_W(IDX_W),
    .RAM_PIPE(RAM_PIPE),
    .EVT_DEPTH(EVT_DEPTH)
)
uut (
    .clk(clk),
    .rst(rst),
    .s_hit_valid(s_hit_valid),
    .s_hit_idx(s_hit_idx),
    .s_hit_len(s_hit_len),
    .s_hit_fin(s_hit_fin),
    .s_hit_rst(s_hit_rst),
    .bubble_req(bubble_req),
    .host_st_valid(host_st_valid),
    .host_st_ready(host_st_ready),
    .host_st_we(host_st_we),
    .host_st_idx(host_st_idx),
    .host_st_wdata(host_st_wdata),
    .host_st_rvalid(host_st_rvalid),
    .host_st_rdata(host_st_rdata),
    .m_evt_valid(m_evt_valid),
    .m_evt_ready(m_evt_ready),
    .m_evt(m_evt),
    .cfg_tick(cfg_tick),
    .cfg_thresh_tcp(cfg_thresh_tcp),
    .cfg_thresh_udp(cfg_thresh_udp),
    .cfg_scan_en(cfg_scan_en),
    .cfg_scan_interval(cfg_scan_interval),
    .clear_start(clear_start),
    .clear_busy(clear_busy),
    .stat_evt_drop(stat_evt_drop)
);

endmodule

`resetall
