// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway parser testbench

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_parser
    import natgw_pkg::*;
#(
    /* verilator lint_off WIDTHTRUNC */
    parameter LANE = 5,
    parameter USER_W = 49
    /* verilator lint_on WIDTHTRUNC */
)
();

logic clk;
logic rst;

taxi_axis_if #(.DATA_W(128), .KEEP_W(16), .USER_EN(1), .USER_W(USER_W)) s_axis(), m_axis_hold();

logic         m_desc_valid;
logic         m_desc_ready;
key_t         m_desc_key_s;
logic         m_desc_lookup;
logic         m_desc_fin;
logic         m_desc_rst;
logic [15:0]  m_desc_len;
meta_t        m_desc_meta_s;
logic         cfg_bypass;

// flat views for cocotb
wire [KEY_W-1:0]  m_desc_key = m_desc_key_s;
wire [META_W-1:0] m_desc_meta = m_desc_meta_s;

natgw_parser #(
    .LANE(LANE),
    .USER_W(USER_W)
)
uut (
    .clk(clk),
    .rst(rst),
    .s_axis(s_axis),
    .m_axis_hold(m_axis_hold),
    .m_desc_valid(m_desc_valid),
    .m_desc_ready(m_desc_ready),
    .m_desc_key(m_desc_key_s),
    .m_desc_lookup(m_desc_lookup),
    .m_desc_fin(m_desc_fin),
    .m_desc_rst(m_desc_rst),
    .m_desc_len(m_desc_len),
    .m_desc_meta(m_desc_meta_s),
    .cfg_bypass(cfg_bypass)
);

endmodule

`resetall
