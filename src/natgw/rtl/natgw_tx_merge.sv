// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: per-lane transmit merge (MAC TX clock domain)

Merges the core's transmit frames and the shim's forwarded frames into one
MAC, a whole frame at a time, round robin. Core frames go out with tid 0 and
shim frames with tid 1; the MAC returns tid in each TX completion, and only
tid 0 completions are passed back to the core.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_tx_merge
(
    input  wire logic  clk,
    input  wire logic  rst,

    taxi_axis_if.snk   s_axis_core,      // from the cndm core
    taxi_axis_if.snk   s_axis_shim,      // forwarded frames (whole frames, no gaps)
    taxi_axis_if.src   m_axis_mac,       // to the MAC (ID_W >= 1)

    taxi_axis_if.snk   s_axis_mac_cpl,   // TX completions from the MAC
    taxi_axis_if.src   m_axis_core_cpl   // TX completions to the core
);

localparam DATA_W = m_axis_mac.DATA_W;
localparam KEEP_W = m_axis_mac.KEEP_W;
localparam USER_W = m_axis_mac.USER_W;
localparam ID_W = m_axis_mac.ID_W;

taxi_axis_if #(
    .DATA_W(DATA_W),
    .KEEP_W(KEEP_W),
    .ID_EN(1),
    .ID_W(ID_W),
    .USER_EN(1),
    .USER_W(USER_W)
) mux_in[2]();

assign mux_in[0].tdata = s_axis_core.tdata;
assign mux_in[0].tkeep = s_axis_core.tkeep;
assign mux_in[0].tstrb = s_axis_core.tkeep;
assign mux_in[0].tvalid = s_axis_core.tvalid;
assign s_axis_core.tready = mux_in[0].tready;
assign mux_in[0].tlast = s_axis_core.tlast;
assign mux_in[0].tid = ID_W'(0);
assign mux_in[0].tdest = '0;
assign mux_in[0].tuser = USER_W'(s_axis_core.tuser);

assign mux_in[1].tdata = s_axis_shim.tdata;
assign mux_in[1].tkeep = s_axis_shim.tkeep;
assign mux_in[1].tstrb = s_axis_shim.tkeep;
assign mux_in[1].tvalid = s_axis_shim.tvalid;
assign s_axis_shim.tready = mux_in[1].tready;
assign mux_in[1].tlast = s_axis_shim.tlast;
assign mux_in[1].tid = ID_W'(1);
assign mux_in[1].tdest = '0;
assign mux_in[1].tuser = USER_W'(s_axis_shim.tuser);

taxi_axis_arb_mux #(
    .S_COUNT(2),
    .UPDATE_TID(1'b0),
    .ARB_ROUND_ROBIN(1'b1),
    .ARB_LSB_HIGH_PRIO(1'b1)
)
mux_inst (
    .clk(clk),
    .rst(rst),
    .s_axis(mux_in),
    .m_axis(m_axis_mac)
);

// completion filter
wire cpl_core = s_axis_mac_cpl.tid == '0;

assign m_axis_core_cpl.tdata = s_axis_mac_cpl.tdata;
assign m_axis_core_cpl.tkeep = s_axis_mac_cpl.tkeep;
assign m_axis_core_cpl.tstrb = s_axis_mac_cpl.tstrb;
assign m_axis_core_cpl.tvalid = s_axis_mac_cpl.tvalid && cpl_core;
assign m_axis_core_cpl.tlast = s_axis_mac_cpl.tlast;
assign m_axis_core_cpl.tid = s_axis_mac_cpl.tid;
assign m_axis_core_cpl.tdest = s_axis_mac_cpl.tdest;
assign m_axis_core_cpl.tuser = s_axis_mac_cpl.tuser;
assign s_axis_mac_cpl.tready = m_axis_core_cpl.tready || !cpl_core;

endmodule

`resetall
