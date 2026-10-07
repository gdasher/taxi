// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: shared lookup engine

Round-robin key arbiter, two CRC hashes, two 4-slot cuckoo tables in URAM,
parallel compare, next-hop table in block RAM. One lookup per clock, fixed
latency, results in order.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_lookup
    import natgw_pkg::*;
#(
    parameter BUCKET_W = 16,
    parameter RAM_PIPE = 3,
    parameter IDX_W = BUCKET_W+3
)
(
    input  wire logic               clk,
    input  wire logic               rst,

    /*
     * Per-lane key inputs
     */
    input  wire logic               s_key_valid[LANES],
    output wire logic               s_key_ready[LANES],
    input  wire key_t               s_key[LANES],
    input  wire logic               s_key_lookup[LANES],
    input  wire logic               s_key_fin[LANES],
    input  wire logic               s_key_rst[LANES],
    input  wire logic [15:0]        s_key_len[LANES],

    /*
     * Results
     */
    output wire logic               m_res_valid,
    output wire logic [2:0]         m_res_lane,
    output wire result_t            m_res,

    /*
     * Hit stream to state block
     */
    output wire logic               m_hit_valid,
    output wire logic [IDX_W-1:0]   m_hit_idx,
    output wire logic [15:0]        m_hit_len,
    output wire logic               m_hit_fin,
    output wire logic               m_hit_rst,

    input  wire logic               bubble_req,
    input  wire logic [15:0]        cfg_bubble_period,
    input  wire logic [31:0]        cfg_seed0,
    input  wire logic [31:0]        cfg_seed1,

    /*
     * Host entry access
     */
    input  wire logic               host_ent_valid,
    output wire logic               host_ent_ready,
    input  wire logic               host_ent_we,
    input  wire logic [IDX_W-1:0]   host_ent_idx,
    input  wire entry_t             host_ent_wdata,
    output wire logic               host_ent_rvalid,
    output wire entry_t             host_ent_rdata,

    /*
     * Host next-hop access
     */
    input  wire logic               host_nh_valid,
    output wire logic               host_nh_ready,
    input  wire logic               host_nh_we,
    input  wire logic [9:0]         host_nh_idx,
    input  wire nh_t                host_nh_wdata,
    output wire logic               host_nh_rvalid,
    output wire nh_t                host_nh_rdata,

    /*
     * Clear
     */
    input  wire logic               clear_start,
    output wire logic               clear_busy
);

localparam SLOTS = 4;
localparam MEMS = 2*SLOTS;
localparam NH_PIPE = 2;
localparam HASH_SPLIT = KEY_W/2;

// partial reflected CRC over key bits [lo, hi)
function automatic logic [31:0] crc_part(input logic [KEY_W-1:0] key, input logic [31:0] crc_in,
        input logic [31:0] poly, input int lo, input int hi);
    logic [31:0] crc;
    crc = crc_in;
    for (int i = lo; i < hi; i++) begin
        if (crc[0] ^ key[i])
            crc = (crc >> 1) ^ poly;
        else
            crc = crc >> 1;
    end
    return crc;
endfunction

// pipeline metadata
typedef struct packed {
    logic        valid;
    logic [2:0]  lane;
    logic        lookup;
    logic        fin;
    logic        rst;
    logic [15:0] len;
} pmeta_t;

/*
 * Stage 0: arbiter
 */
logic [2:0] rr_reg = '0;
logic [15:0] bubble_cnt_reg = '0;
logic stall;
logic grant_valid;
logic [2:0] grant_lane;

always_comb begin
    stall = bubble_req && cfg_bubble_period != 0 && bubble_cnt_reg >= cfg_bubble_period - 16'd1;

    grant_valid = 1'b0;
    grant_lane = '0;
    for (int k = LANES-1; k >= 0; k--) begin
        // highest priority = rr_reg, searched last so it overrides
        if (s_key_valid[3'(rr_reg + 3'(k))]) begin
            grant_valid = 1'b1;
            grant_lane = 3'(rr_reg + 3'(k));
        end
    end
    if (stall) begin
        grant_valid = 1'b0;
    end
end

for (genvar n = 0; n < LANES; n = n + 1) begin : ready
    assign s_key_ready[n] = grant_valid && grant_lane == 3'(n);
end

pmeta_t s0_meta_reg = '0;
key_t s0_key_reg = '0;

always_ff @(posedge clk) begin
    if (stall) begin
        bubble_cnt_reg <= '0;
    end else if (bubble_cnt_reg != 16'hffff) begin
        bubble_cnt_reg <= bubble_cnt_reg + 1;
    end

    s0_meta_reg.valid <= grant_valid;
    s0_meta_reg.lane <= grant_lane;
    s0_meta_reg.lookup <= s_key_lookup[grant_lane];
    s0_meta_reg.fin <= s_key_fin[grant_lane];
    s0_meta_reg.rst <= s_key_rst[grant_lane];
    s0_meta_reg.len <= s_key_len[grant_lane];
    s0_key_reg <= s_key[grant_lane];

    if (grant_valid) begin
        rr_reg <= grant_lane + 1;
    end

    if (rst) begin
        rr_reg <= '0;
        bubble_cnt_reg <= '0;
        s0_meta_reg.valid <= 1'b0;
    end
end

/*
 * Stages 1-2: hash
 */
pmeta_t s1_meta_reg = '0, s2_meta_reg = '0;
key_t s1_key_reg = '0, s2_key_reg = '0;
logic [31:0] s1_h0_reg = '0, s1_h1_reg = '0;
logic [31:0] s2_h0_reg = '0, s2_h1_reg = '0;

always_ff @(posedge clk) begin
    s1_meta_reg <= s0_meta_reg;
    s1_key_reg <= s0_key_reg;
    s1_h0_reg <= crc_part(s0_key_reg, cfg_seed0, POLY_CRC32C, 0, HASH_SPLIT);
    s1_h1_reg <= crc_part(s0_key_reg, cfg_seed1, POLY_CRC32, 0, HASH_SPLIT);

    s2_meta_reg <= s1_meta_reg;
    s2_key_reg <= s1_key_reg;
    s2_h0_reg <= crc_part(s1_key_reg, s1_h0_reg, POLY_CRC32C, HASH_SPLIT, KEY_W);
    s2_h1_reg <= crc_part(s1_key_reg, s1_h1_reg, POLY_CRC32, HASH_SPLIT, KEY_W);

    if (rst) begin
        s1_meta_reg.valid <= 1'b0;
        s2_meta_reg.valid <= 1'b0;
    end
end

/*
 * Table memories
 */
// clear walks every table row and every next-hop entry
localparam CLR_W = BUCKET_W > NH_IDX_W ? BUCKET_W : NH_IDX_W;

logic clear_busy_reg = 1'b0;
logic [CLR_W-1:0] clear_row_reg = '0;

wire host_ent_fire = host_ent_valid && host_ent_ready;
wire host_ent_t = host_ent_idx[IDX_W-1];
wire [BUCKET_W-1:0] host_ent_bucket = host_ent_idx[BUCKET_W+1:2];
wire [1:0] host_ent_slot = host_ent_idx[1:0];

assign host_ent_ready = !clear_busy_reg;
assign clear_busy = clear_busy_reg;

entry_t ram_a_dout[MEMS];
entry_t ram_b_dout[MEMS];

for (genvar m = 0; m < MEMS; m = m + 1) begin : mem

    wire sel = host_ent_fire && {host_ent_t, host_ent_slot} == 3'(m);

    natgw_ram #(
        .DATA_W(ENTRY_W),
        .ADDR_W(BUCKET_W),
        .PIPE(RAM_PIPE),
        .RAM_STYLE("ultra")
    )
    ram_inst (
        .clk(clk),

        .a_en(1'b1),
        .a_addr(m < SLOTS ? s2_h0_reg[BUCKET_W-1:0] : s2_h1_reg[BUCKET_W-1:0]),
        .a_dout(ram_a_dout[m]),

        .b_en(clear_busy_reg || sel),
        .b_we(clear_busy_reg || host_ent_we),
        .b_addr(clear_busy_reg ? clear_row_reg[BUCKET_W-1:0] : host_ent_bucket),
        .b_din(clear_busy_reg ? '0 : host_ent_wdata),
        .b_dout(ram_b_dout[m])
    );

end

always_ff @(posedge clk) begin
    if (clear_busy_reg) begin
        clear_row_reg <= clear_row_reg + 1;
        if (&clear_row_reg) begin
            clear_busy_reg <= 1'b0;
        end
    end else if (clear_start) begin
        clear_busy_reg <= 1'b1;
        clear_row_reg <= '0;
    end

    if (rst) begin
        clear_busy_reg <= 1'b0;
        clear_row_reg <= '0;
    end
end

// host entry read return
logic [RAM_PIPE-1:0] host_rd_valid_pipe = '0;
logic [2:0] host_rd_mem_pipe[RAM_PIPE];
logic host_ent_rvalid_reg = 1'b0;
entry_t host_ent_rdata_reg = '0;

always_ff @(posedge clk) begin
    host_rd_valid_pipe <= {host_rd_valid_pipe[RAM_PIPE-2:0], host_ent_fire && !host_ent_we};
    host_rd_mem_pipe[0] <= {host_ent_t, host_ent_slot};
    for (int i = 1; i < RAM_PIPE; i++) begin
        host_rd_mem_pipe[i] <= host_rd_mem_pipe[i-1];
    end

    host_ent_rvalid_reg <= host_rd_valid_pipe[RAM_PIPE-1];
    host_ent_rdata_reg <= ram_b_dout[host_rd_mem_pipe[RAM_PIPE-1]];

    if (rst) begin
        host_rd_valid_pipe <= '0;
        host_ent_rvalid_reg <= 1'b0;
    end
end

assign host_ent_rvalid = host_ent_rvalid_reg;
assign host_ent_rdata = host_ent_rdata_reg;

/*
 * Delay metadata to RAM output
 */
pmeta_t d_meta[RAM_PIPE];
key_t d_key[RAM_PIPE];
logic [31:0] d_h0[RAM_PIPE];
logic [BUCKET_W-1:0] d_b0[RAM_PIPE];
logic [BUCKET_W-1:0] d_b1[RAM_PIPE];

always_ff @(posedge clk) begin
    d_meta[0] <= s2_meta_reg;
    d_key[0] <= s2_key_reg;
    d_h0[0] <= s2_h0_reg;
    d_b0[0] <= s2_h0_reg[BUCKET_W-1:0];
    d_b1[0] <= s2_h1_reg[BUCKET_W-1:0];
    for (int i = 1; i < RAM_PIPE; i++) begin
        d_meta[i] <= d_meta[i-1];
        d_key[i] <= d_key[i-1];
        d_h0[i] <= d_h0[i-1];
        d_b0[i] <= d_b0[i-1];
        d_b1[i] <= d_b1[i-1];
    end

    if (rst) begin
        for (int i = 0; i < RAM_PIPE; i++) begin
            d_meta[i].valid <= 1'b0;
        end
    end
end

/*
 * Compare stage
 */
typedef struct packed {
    logic        xlate_dst;
    logic [31:0] new_ip;
    logic [15:0] new_port;
    logic        dec_ttl;
    logic [9:0]  nh_idx;
} action_t;

pmeta_t c_meta_reg = '0;
logic [31:0] c_h0_reg = '0;
logic [BUCKET_W-1:0] c_b0_reg = '0, c_b1_reg = '0;
logic [MEMS-1:0] c_match_reg = '0;
action_t c_act_reg[MEMS];

always_ff @(posedge clk) begin
    c_meta_reg <= d_meta[RAM_PIPE-1];
    c_h0_reg <= d_h0[RAM_PIPE-1];
    c_b0_reg <= d_b0[RAM_PIPE-1];
    c_b1_reg <= d_b1[RAM_PIPE-1];
    for (int m = 0; m < MEMS; m++) begin
        c_match_reg[m] <= ram_a_dout[m].valid && ram_a_dout[m].key == d_key[RAM_PIPE-1];
        c_act_reg[m].xlate_dst <= ram_a_dout[m].xlate_dst;
        c_act_reg[m].new_ip <= ram_a_dout[m].new_ip;
        c_act_reg[m].new_port <= ram_a_dout[m].new_port;
        c_act_reg[m].dec_ttl <= ram_a_dout[m].dec_ttl;
        c_act_reg[m].nh_idx <= ram_a_dout[m].nh_idx;
    end

    if (rst) begin
        c_meta_reg.valid <= 1'b0;
    end
end

/*
 * Select stage
 */
pmeta_t p_meta_reg = '0;
logic p_hit_reg = 1'b0;
logic [IDX_W-1:0] p_idx_reg = '0;
action_t p_act_reg = '0;
logic [31:0] p_h0_reg = '0;

always_ff @(posedge clk) begin
    logic hit;
    logic [2:0] sel;

    hit = 1'b0;
    sel = '0;
    for (int m = MEMS-1; m >= 0; m--) begin
        if (c_match_reg[m]) begin
            hit = 1'b1;
            sel = 3'(m);
        end
    end
    hit = hit && c_meta_reg.lookup;

    p_meta_reg <= c_meta_reg;
    p_hit_reg <= hit;
    p_idx_reg <= hit ? {sel[2], sel[2] ? c_b1_reg : c_b0_reg, sel[1:0]} : '0;
    p_act_reg <= hit ? c_act_reg[sel] : '0;
    p_h0_reg <= c_meta_reg.lookup ? c_h0_reg : '0;

    if (rst) begin
        p_meta_reg.valid <= 1'b0;
        p_hit_reg <= 1'b0;
    end
end

/*
 * Next-hop table
 */
nh_t nh_a_dout;
nh_t nh_b_dout;

natgw_ram #(
    .DATA_W(NH_W),
    .ADDR_W(NH_IDX_W),
    .PIPE(NH_PIPE),
    .RAM_STYLE("block")
)
nh_ram_inst (
    .clk(clk),

    .a_en(1'b1),
    .a_addr(p_act_reg.nh_idx),
    .a_dout(nh_a_dout),

    .b_en(clear_busy_reg || host_nh_valid),
    .b_we(clear_busy_reg || host_nh_we),
    .b_addr(clear_busy_reg ? clear_row_reg[NH_IDX_W-1:0] : host_nh_idx),
    .b_din(clear_busy_reg ? '0 : host_nh_wdata),
    .b_dout(nh_b_dout)
);

assign host_nh_ready = !clear_busy_reg;

logic [NH_PIPE-1:0] nh_rd_pipe = '0;

always_ff @(posedge clk) begin
    nh_rd_pipe <= {nh_rd_pipe[NH_PIPE-2:0], host_nh_valid && host_nh_ready && !host_nh_we};
    if (rst) begin
        nh_rd_pipe <= '0;
    end
end

assign host_nh_rvalid = nh_rd_pipe[NH_PIPE-1];
assign host_nh_rdata = nh_b_dout;

pmeta_t n_meta[NH_PIPE];
logic n_hit[NH_PIPE];
logic [IDX_W-1:0] n_idx[NH_PIPE];
action_t n_act[NH_PIPE];
logic [31:0] n_h0[NH_PIPE];

always_ff @(posedge clk) begin
    n_meta[0] <= p_meta_reg;
    n_hit[0] <= p_hit_reg;
    n_idx[0] <= p_idx_reg;
    n_act[0] <= p_act_reg;
    n_h0[0] <= p_h0_reg;
    for (int i = 1; i < NH_PIPE; i++) begin
        n_meta[i] <= n_meta[i-1];
        n_hit[i] <= n_hit[i-1];
        n_idx[i] <= n_idx[i-1];
        n_act[i] <= n_act[i-1];
        n_h0[i] <= n_h0[i-1];
    end

    if (rst) begin
        for (int i = 0; i < NH_PIPE; i++) begin
            n_meta[i].valid <= 1'b0;
        end
    end
end

/*
 * Output stage
 */
logic out_valid_reg = 1'b0;
logic [2:0] out_lane_reg = '0;
result_t out_res_reg = '0;
logic out_hit_valid_reg = 1'b0;
logic [15:0] out_len_reg = '0;
logic out_fin_reg = 1'b0;
logic out_rst_reg = 1'b0;

always_ff @(posedge clk) begin
    out_valid_reg <= n_meta[NH_PIPE-1].valid;
    out_lane_reg <= n_meta[NH_PIPE-1].lane;
    out_res_reg.hit <= n_hit[NH_PIPE-1];
    out_res_reg.idx <= 32'(n_idx[NH_PIPE-1]);
    out_res_reg.xlate_dst <= n_act[NH_PIPE-1].xlate_dst;
    out_res_reg.new_ip <= n_act[NH_PIPE-1].new_ip;
    out_res_reg.new_port <= n_act[NH_PIPE-1].new_port;
    out_res_reg.dec_ttl <= n_act[NH_PIPE-1].dec_ttl;
    out_res_reg.nh_idx <= n_act[NH_PIPE-1].nh_idx;
    out_res_reg.nh <= n_hit[NH_PIPE-1] ? nh_a_dout : '0;
    out_res_reg.hash <= n_h0[NH_PIPE-1];
    out_hit_valid_reg <= n_meta[NH_PIPE-1].valid && n_hit[NH_PIPE-1];
    out_len_reg <= n_meta[NH_PIPE-1].len;
    out_fin_reg <= n_meta[NH_PIPE-1].fin;
    out_rst_reg <= n_meta[NH_PIPE-1].rst;

    if (rst) begin
        out_valid_reg <= 1'b0;
        out_hit_valid_reg <= 1'b0;
    end
end

assign m_res_valid = out_valid_reg;
assign m_res_lane = out_lane_reg;
assign m_res = out_res_reg;

assign m_hit_valid = out_hit_valid_reg;
assign m_hit_idx = IDX_W'(out_res_reg.idx);
assign m_hit_len = out_len_reg;
assign m_hit_fin = out_fin_reg;
assign m_hit_rst = out_rst_reg;

endmodule

`resetall
