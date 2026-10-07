// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: natgw_rewrite testbench

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_rewrite
    import natgw_pkg::*;
#(
    /* verilator lint_off WIDTHTRUNC */
    parameter LANE = 2,
    parameter USER_W = 49
    /* verilator lint_on WIDTHTRUNC */
)
();

logic clk;
logic rst;

taxi_axis_if #(.DATA_W(128), .KEEP_EN(1), .KEEP_W(16), .LAST_EN(1), .USER_EN(1), .USER_W(USER_W)) s_axis_hold();
taxi_axis_if #(.DATA_W(128), .KEEP_EN(1), .KEEP_W(16), .LAST_EN(1), .DEST_EN(1), .DEST_W(3), .USER_EN(1), .USER_W(1)) m_axis_fwd();
taxi_axis_if #(.DATA_W(128), .KEEP_EN(1), .KEEP_W(16), .LAST_EN(1), .USER_EN(1), .USER_W(USER_W)) m_axis_punt();

logic                s_res_valid;
logic                s_res_ready;
logic [RESULT_W-1:0] s_res;

logic                s_meta_valid;
logic                s_meta_ready;
logic [META_W-1:0]   s_meta;

logic                cfg_punt_hdr;
logic [7:0]          cfg_egress_en;

logic                stat_valid;
logic [7:0]          stat_reason;

natgw_rewrite #(
    .LANE(LANE),
    .USER_W(USER_W)
)
uut (
    .clk(clk),
    .rst(rst),

    .s_axis_hold(s_axis_hold),

    .s_res_valid(s_res_valid),
    .s_res_ready(s_res_ready),
    .s_res(result_t'(s_res)),

    .s_meta_valid(s_meta_valid),
    .s_meta_ready(s_meta_ready),
    .s_meta(meta_t'(s_meta)),

    .m_axis_fwd(m_axis_fwd),
    .m_axis_punt(m_axis_punt),

    .cfg_punt_hdr(cfg_punt_hdr),
    .cfg_egress_en(cfg_egress_en),

    .stat_valid(stat_valid),
    .stat_reason(stat_reason)
);

endmodule

`resetall
