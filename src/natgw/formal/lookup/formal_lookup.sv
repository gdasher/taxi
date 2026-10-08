// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_lookup

All eight lanes present arbitrary keys every cycle; the host writes arbitrary
entries and next hops. One key K (any value) and one next-hop index N are
tracked against a shadow of the slots K can occupy (its four T0 slots and
four T1 slots) and of next hop N, updated when the host's writes are
accepted. A free input `freeze` ends the period in which the shadow is built.

  P4.1  every accepted key yields exactly one result, LAT cycles later, with
        its lane; no result otherwise; the hit stream fires exactly for
        looked-up hits, with the same index
  P4.2  (task stable) the tables start empty (as after the power-up clear);
        for K accepted after freeze, with K's slots and next hop N not
        written after freeze: hit, index
        (first valid match, T0 slots 0..3 then T1), action, next hop and
        hash equal the specification; lookup=0 gives a miss and hash 0
  P4.3  (task reloc) host writes to K's slots continue after freeze, under
        the host protocol invariant (at every cycle at least one candidate
        slot holds K valid, and every slot holding K valid carries action A):
        K always hits, with action A (no miss while an entry is relocated)

*/

`default_nettype none

module formal_lookup
    import natgw_pkg::*;
#(
    parameter BUCKET_W = 1,
    parameter RAM_PIPE = 2,
    parameter logic RELOC = 1'b0,
    parameter ACTIVE_LANES = 8     // lanes that may present keys (the rest stay idle)
)
(
    input  wire logic        clk,

    input  wire logic        key_valid[LANES],
    input  wire key_t        key_data[LANES],
    input  wire logic        key_lookup[LANES],
    input  wire logic        key_fin[LANES],
    input  wire logic        key_rst[LANES],
    input  wire logic [15:0] key_len[LANES],
    input  wire logic        bubble_req,
    input  wire logic [15:0] bubble_period,

    input  wire logic        ent_valid,
    input  wire logic        ent_we,
    input  wire logic [BUCKET_W+2:0] ent_idx,
    input  wire entry_t      ent_wdata,
    input  wire logic        nh_valid,
    input  wire logic        nh_we,
    input  wire logic [9:0]  nh_idx,
    input  wire nh_t         nh_wdata,

    input  wire logic        freeze,
    input  wire logic        trk
);

localparam IDX_W = BUCKET_W + 3;
localparam LAT = 8 + RAM_PIPE;

key_t        K;
logic [9:0]  N;
logic [31:0] seed0;
logic [31:0] seed1;
logic [15+1+32+1+10:0] A;   // {dec_ttl, new_port, new_ip, xlate_dst, nh_idx} for the reloc task

// arbitrary constants: no initial value, held for the whole trace
// (the slang frontend does not honour (* anyconst *))
always_ff @(posedge clk) begin
    K <= K;
    N <= N;
    seed0 <= seed0;
    seed1 <= seed1;
    A <= A;
end

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

// ---------------------------------------------------------------- DUT

wire        key_ready[LANES];
wire        res_valid;
wire [2:0]  res_lane;
result_t    res;
wire        hit_valid;
wire [IDX_W-1:0] hit_idx;
wire [15:0] hit_len;
wire        hit_fin, hit_rst;
wire        ent_ready, ent_rvalid, nh_ready, nh_rvalid;
entry_t     ent_rdata;
nh_t        nh_rdata;
wire        clear_busy;

natgw_lookup #(
    .BUCKET_W(BUCKET_W),
    .RAM_PIPE(RAM_PIPE)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_key_valid(key_valid),
    .s_key_ready(key_ready),
    .s_key(key_data),
    .s_key_lookup(key_lookup),
    .s_key_fin(key_fin),
    .s_key_rst(key_rst),
    .s_key_len(key_len),
    .m_res_valid(res_valid),
    .m_res_lane(res_lane),
    .m_res(res),
    .m_hit_valid(hit_valid),
    .m_hit_idx(hit_idx),
    .m_hit_len(hit_len),
    .m_hit_fin(hit_fin),
    .m_hit_rst(hit_rst),
    .bubble_req(bubble_req),
    .cfg_bubble_period(bubble_period),
    .cfg_seed0(seed0),
    .cfg_seed1(seed1),
    .host_ent_valid(ent_valid),
    .host_ent_ready(ent_ready),
    .host_ent_we(ent_we),
    .host_ent_idx(ent_idx),
    .host_ent_wdata(ent_wdata),
    .host_ent_rvalid(ent_rvalid),
    .host_ent_rdata(ent_rdata),
    .host_nh_valid(nh_valid),
    .host_nh_ready(nh_ready),
    .host_nh_we(nh_we),
    .host_nh_idx(nh_idx),
    .host_nh_wdata(nh_wdata),
    .host_nh_rvalid(nh_rvalid),
    .host_nh_rdata(nh_rdata),
    .clear_start(1'b0),
    .clear_busy(clear_busy)
);

// ---------------------------------------------------------------- environment

// lanes beyond ACTIVE_LANES stay idle
always_comb begin
    for (int l = ACTIVE_LANES; l < LANES; l++) assume (!key_valid[l]);
end

// valid/ready rules on the key inputs: a presented key stays until taken
logic p_key_valid[LANES];
key_t p_key_data[LANES];
logic p_key_lookup[LANES];
logic p_key_ready[LANES];

always_ff @(posedge clk) begin
    for (int l = 0; l < LANES; l++) begin
        p_key_valid[l] <= key_valid[l];
        p_key_data[l] <= key_data[l];
        p_key_lookup[l] <= key_lookup[l];
        p_key_ready[l] <= key_ready[l];
    end
end

always_comb begin
    if (!rst) begin
        for (int l = 0; l < LANES; l++) begin
            if (p_key_valid[l] && !p_key_ready[l]) begin
                assume (key_valid[l]);
                assume (key_data[l] == p_key_data[l]);
                assume (key_lookup[l] == p_key_lookup[l]);
            end
        end
    end
end

// freeze is a one-way switch
logic frozen = 1'b0;
logic [3:0] since_freeze = '0;
always_ff @(posedge clk) begin
    if (freeze) frozen <= 1'b1;
    if (frozen && since_freeze != 4'hf) since_freeze <= since_freeze + 1;
    if (rst) begin
        frozen <= 1'b0;
        since_freeze <= '0;
    end
end
always_comb if (!rst && frozen) assume (freeze);

// K's candidate buckets
wire [BUCKET_W-1:0] kb0 = key_crc(K, seed0, POLY_CRC32C)[BUCKET_W-1:0];
wire [BUCKET_W-1:0] kb1 = key_crc(K, seed1, POLY_CRC32)[BUCKET_W-1:0];

function automatic logic is_cand(input logic [IDX_W-1:0] i);
    return i[IDX_W-1] ? i[BUCKET_W+1:2] == kb1 : i[BUCKET_W+1:2] == kb0;
endfunction

function automatic int cand_slot(input logic [IDX_W-1:0] i);
    return int'(i[IDX_W-1]) * 4 + int'(i[1:0]);
endfunction

// ---------------------------------------------------------------- shadow

logic   sh_known[8];
entry_t sh_ent[8];
logic   sh_nh_known = 1'b0;
nh_t    sh_nh;

wire ent_wr = ent_valid && ent_ready && ent_we;
wire nh_wr = nh_valid && nh_ready && nh_we;

always_ff @(posedge clk) begin
    if (ent_wr && is_cand(ent_idx)) begin
        sh_known[cand_slot(ent_idx)] <= 1'b1;
        sh_ent[cand_slot(ent_idx)] <= ent_wdata;
    end
    if (nh_wr && nh_idx == N) begin
        sh_nh_known <= 1'b1;
        sh_nh <= nh_wdata;
    end
    if (rst) begin
        // the tables start empty (assumed below), so every slot is known
        for (int c = 0; c < 8; c++) begin
            sh_known[c] <= 1'b1;
            sh_ent[c] <= '0;
        end
        sh_nh_known <= 1'b0;
    end
end

wire all_known = sh_known[0] && sh_known[1] && sh_known[2] && sh_known[3] &&
                 sh_known[4] && sh_known[5] && sh_known[6] && sh_known[7];

// tables start empty, as after the power-up clear (lets the bound reach
// tracked lookups instead of spending cycles on setup writes)
for (genvar m = 0; m < 8; m = m + 1) begin : init_mem
    always_comb begin
        if (rst) begin
            for (int r = 0; r < 2**BUCKET_W; r++) assume (dut.mem[m].ram_inst.bank[0].mem[r] == '0);
        end
    end
end

// no reads or clears in this harness (covered in simulation); nothing is
// presented during reset
always_comb begin
    if (rst) begin
        assume (!ent_valid && !nh_valid);
        for (int l = 0; l < LANES; l++) assume (!key_valid[l]);
    end
    if (ent_valid) assume (ent_we);
    if (nh_valid) assume (nh_we);
end

// next hop N is frozen after freeze in both tasks
always_comb if (!rst && frozen) assume (!(nh_valid && nh_idx == N));

if (!RELOC) begin : stable
    // K's slots are not written after freeze
    always_comb if (!rst && frozen) assume (!(ent_valid && is_cand(ent_idx)));
end else begin : reloc
    // host protocol invariant on the shadow from freeze on
    logic any_k;
    always_comb begin
        any_k = 1'b0;
        for (int c = 0; c < 8; c++) begin
            if (sh_ent[c].valid && sh_ent[c].key == K) begin
                any_k = 1'b1;
                if (!rst && frozen) assume ({sh_ent[c].dec_ttl, sh_ent[c].new_port, sh_ent[c].new_ip, sh_ent[c].xlate_dst, sh_ent[c].nh_idx} == A);
            end
        end
        if (!rst && frozen) begin
            assume (all_known);
            assume (any_k);
        end
    end
end

// ---------------------------------------------------------------- P4.1 order and latency

logic       acc_any;
logic [2:0] acc_lane;
key_t       acc_key;
logic       acc_lookup;

always_comb begin
    acc_any = 1'b0;
    acc_lane = '0;
    acc_key = '0;
    acc_lookup = 1'b0;
    for (int l = 0; l < LANES; l++) begin
        if (key_valid[l] && key_ready[l]) begin
            if (!rst) assert (!acc_any);    // at most one accept per cycle
            acc_any = 1'b1;
            acc_lane = 3'(l);
            acc_key = key_data[l];
            acc_lookup = key_lookup[l];
        end
    end
end

logic       q_valid[LAT];
logic [2:0] q_lane[LAT];
logic       q_lookup[LAT];
logic       q_trk[LAT];

// a write accepted before freeze lands in the RAM two cycles later; the lookup
// reads the RAM three cycles after accepting a key, so one cycle of margin suffices
wire trk_now = !rst && acc_any && trk && acc_key == K && since_freeze >= 4'd1;

always_ff @(posedge clk) begin
    q_valid[0] <= !rst && acc_any;
    q_lane[0] <= acc_lane;
    q_lookup[0] <= acc_lookup;
    q_trk[0] <= trk_now;
    for (int i = 1; i < LAT; i++) begin
        q_valid[i] <= q_valid[i-1];
        q_lane[i] <= q_lane[i-1];
        q_lookup[i] <= q_lookup[i-1];
        q_trk[i] <= q_trk[i-1];
    end
    if (rst) for (int i = 0; i < LAT; i++) q_valid[i] <= 1'b0;
end

always_comb begin
    if (!rst) begin
        assert (res_valid == q_valid[LAT-1]);
        if (res_valid) begin
            assert (res_lane == q_lane[LAT-1]);
            if (!q_lookup[LAT-1]) assert (!res.hit && res.hash == 32'd0);
        end
        assert (hit_valid == (res_valid && q_lookup[LAT-1] && res.hit));
        if (hit_valid) assert (32'(hit_idx) == res.idx);
    end
end

// ---------------------------------------------------------------- P4.2 / P4.3 tracked key

// specification lookup over the shadow: first valid copy of K, T0 slots 0..3 then T1
function automatic logic [4:0] spec_lookup(input entry_t e[8], input key_t k);
    for (int c = 0; c < 8; c++) begin
        if (e[c].valid && e[c].key == k) return {1'b1, 4'(c)};
    end
    return 5'd0;
endfunction

wire [4:0] sp_res = spec_lookup(sh_ent, K);
wire       sp_hit = sp_res[4];
wire [3:0] sp_c = sp_res[3:0];

wire [IDX_W-1:0] sp_idx = {sp_c[2], (sp_c[2] ? kb1 : kb0), sp_c[1:0]};
wire entry_t sp_ent = sh_ent[sp_c[2:0]];

always_comb begin
    if (!rst && res_valid && q_trk[LAT-1] && q_lookup[LAT-1]) begin
        if (!RELOC) begin
            if (all_known) begin
                assert (res.hit == sp_hit);
                assert (res.hash == key_crc(K, seed0, POLY_CRC32C));
                if (sp_hit) begin
                    assert (res.idx == 32'(sp_idx));
                    assert (res.xlate_dst == sp_ent.xlate_dst);
                    assert (res.new_ip == sp_ent.new_ip);
                    assert (res.new_port == sp_ent.new_port);
                    assert (res.dec_ttl == sp_ent.dec_ttl);
                    assert (res.nh_idx == sp_ent.nh_idx);
                    if (sp_ent.nh_idx == N && sh_nh_known) assert (res.nh == sh_nh);
                end
            end
        end else begin
            assert (res.hit);
            assert ({res.dec_ttl, res.new_port, res.new_ip, res.xlate_dst, res.nh_idx} == A);
        end
    end
end

always_ff @(posedge clk) begin
    if (!rst) cover (res_valid && q_trk[LAT-1] && q_lookup[LAT-1] && res.hit);
    if (!rst) cover (res_valid && q_trk[LAT-1] && q_lookup[LAT-1] && !res.hit && all_known);
end

endmodule
