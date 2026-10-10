// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway lookup engine testbench wrapper (flattens array and struct ports)

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_lookup
    import natgw_pkg::*;
#(
    /* verilator lint_off WIDTHTRUNC */
    parameter BUCKET_W = 6,
    parameter RAM_PIPE = 3
    /* verilator lint_on WIDTHTRUNC */
)
();

localparam IDX_W = BUCKET_W+3;

logic clk;
logic rst;

logic [LANES-1:0] s_key_valid;
logic [LANES-1:0] s_key_ready;
logic [LANES*KEY_W-1:0] s_key;
logic [LANES-1:0] s_key_lookup;
logic [LANES-1:0] s_key_fin;
logic [LANES-1:0] s_key_rst;
logic [LANES*16-1:0] s_key_len;

logic m_res_valid;
logic [2:0] m_res_lane;
logic [RESULT_W-1:0] m_res;
logic [natgw_pkg::KEY_W-1:0] m_res_key;
logic [31:0] m_res_h1;
logic m_res_lookup;

logic m_hit_valid;
logic [IDX_W-1:0] m_hit_idx;
logic [15:0] m_hit_len;
logic m_hit_fin;
logic m_hit_rst;

logic bubble_req;
logic [15:0] cfg_bubble_period;
logic [31:0] cfg_seed0;
logic [31:0] cfg_seed1;

logic host_ent_valid;
logic host_ent_ready;
logic host_ent_we;
logic [IDX_W-1:0] host_ent_idx;
logic [ENTRY_W-1:0] host_ent_wdata;
logic host_ent_rvalid;
logic [ENTRY_W-1:0] host_ent_rdata;

logic host_nh_valid;
logic host_nh_ready;
logic host_nh_we;
logic [9:0] host_nh_idx;
logic [NH_W-1:0] host_nh_wdata;
logic host_nh_rvalid;
logic [NH_W-1:0] host_nh_rdata;

logic clear_start;
logic clear_busy;

logic s_key_valid_a[LANES];
logic s_key_ready_a[LANES];
key_t s_key_a[LANES];
logic s_key_lookup_a[LANES];
logic s_key_fin_a[LANES];
logic s_key_rst_a[LANES];
logic [15:0] s_key_len_a[LANES];

for (genvar n = 0; n < LANES; n = n + 1) begin : lane
    assign s_key_valid_a[n] = s_key_valid[n];
    assign s_key_ready[n] = s_key_ready_a[n];
    assign s_key_a[n] = s_key[n*KEY_W +: KEY_W];
    assign s_key_lookup_a[n] = s_key_lookup[n];
    assign s_key_fin_a[n] = s_key_fin[n];
    assign s_key_rst_a[n] = s_key_rst[n];
    assign s_key_len_a[n] = s_key_len[n*16 +: 16];
end

natgw_lookup #(
    .BUCKET_W(BUCKET_W),
    .RAM_PIPE(RAM_PIPE),
    .IDX_W(IDX_W)
)
uut (
    .clk(clk),
    .rst(rst),

    .s_key_valid(s_key_valid_a),
    .s_key_ready(s_key_ready_a),
    .s_key(s_key_a),
    .s_key_lookup(s_key_lookup_a),
    .s_key_fin(s_key_fin_a),
    .s_key_rst(s_key_rst_a),
    .s_key_len(s_key_len_a),

    .m_res_valid(m_res_valid),
    .m_res_lane(m_res_lane),
    .m_res(m_res),
    .m_res_key(m_res_key),
    .m_res_h1(m_res_h1),
    .m_res_lookup(m_res_lookup),

    .m_hit_valid(m_hit_valid),
    .m_hit_idx(m_hit_idx),
    .m_hit_len(m_hit_len),
    .m_hit_fin(m_hit_fin),
    .m_hit_rst(m_hit_rst),

    .bubble_req(bubble_req),
    .cfg_bubble_period(cfg_bubble_period),
    .cfg_seed0(cfg_seed0),
    .cfg_seed1(cfg_seed1),

    .host_ent_valid(host_ent_valid),
    .host_ent_ready(host_ent_ready),
    .host_ent_we(host_ent_we),
    .host_ent_idx(host_ent_idx),
    .host_ent_wdata(host_ent_wdata),
    .host_ent_rvalid(host_ent_rvalid),
    .host_ent_rdata(host_ent_rdata),

    .host_nh_valid(host_nh_valid),
    .host_nh_ready(host_nh_ready),
    .host_nh_we(host_nh_we),
    .host_nh_idx(host_nh_idx),
    .host_nh_wdata(host_nh_wdata),
    .host_nh_rvalid(host_nh_rvalid),
    .host_nh_rdata(host_nh_rdata),

    .clear_start(clear_start),
    .clear_busy(clear_busy)
);

endmodule

`resetall
