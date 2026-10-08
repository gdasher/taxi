// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_parser against an independent statement of the classification
spec (the same rules as natgw_model.parse(), re-written here from the design
plan, not from the parser RTL).

Two frames of arbitrary content and length (1..MAXB bytes) are driven
back to back with arbitrary idle cycles, and both outputs see arbitrary
backpressure. Checked:
  P2.1  every hold-output beat equals the input beat (data, keep, last, user)
  P2.2  exactly one descriptor per frame, in order
  P2.3  descriptor key, lookup, fin, rst, len and every meta field equal the reference
  P2.4  outputs hold steady while stalled (valid/ready rules)

*/

`default_nettype none

module formal_parser
    import natgw_pkg::*;
    import natgw_formal_ref_pkg::*;
#(
    parameter LANE = 5
)
(
    input wire logic clk,
    input wire logic in_go,         // free: source may present a beat
    input wire logic hold_ready,    // free: backpressure on the hold output
    input wire logic desc_ready     // free: backpressure on the descriptor output
);

localparam BEATS = (MAXB + 15) / 16;

logic [7:0] frame_a[MAXB];
logic [7:0] frame_b[MAXB];
logic [6:0] len_a;
logic [6:0] len_b;
logic       bypass;
logic [1:0] user_a;
logic [1:0] user_b;

// arbitrary constants: no initial value, held for the whole trace
// (the slang frontend does not honour (* anyconst *))
always_ff @(posedge clk) begin
    frame_a <= frame_a;
    frame_b <= frame_b;
    len_a <= len_a;
    len_b <= len_b;
    bypass <= bypass;
    user_a <= user_a;
    user_b <= user_b;
end

always_comb begin
    assume (len_a >= 1 && len_a <= MAXB);
    assume (len_b >= 1 && len_b <= MAXB);
end

// reset for the first cycle
logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

// ---------------------------------------------------------------- reference

wire ref_t ref_a = reference(frame_a, len_a, bypass, 3'(LANE));
wire ref_t ref_b = reference(frame_b, len_b, bypass, 3'(LANE));

// ---------------------------------------------------------------- source

taxi_axis_if #(.DATA_W(128), .USER_EN(1), .USER_W(2)) s_axis(), hold_axis();

function automatic logic [127:0] beat_data(input logic [7:0] f[MAXB], input int b);
    logic [127:0] d;
    for (int k = 0; k < 16; k++) d[8*k +: 8] = (16*b + k < MAXB) ? f[16*b + k] : 8'd0;
    return d;
endfunction

function automatic logic [15:0] beat_keep(input logic [6:0] l, input int b);
    logic [15:0] m;
    for (int k = 0; k < 16; k++) m[k] = 16*b + k < int'(l);
    return m;
endfunction

logic       src_frame = 1'b0;     // 0 = frame a, 1 = frame b
logic [2:0] src_beat = '0;
logic       src_done = 1'b0;
logic       src_valid = 1'b0;

wire [6:0]  src_len = src_frame ? len_b : len_a;
wire        src_last = 16*(int'(src_beat)+1) >= int'(src_len);

assign s_axis.tdata = src_frame ? beat_data(frame_b, int'(src_beat)) : beat_data(frame_a, int'(src_beat));
assign s_axis.tkeep = beat_keep(src_len, int'(src_beat));
assign s_axis.tstrb = s_axis.tkeep;
assign s_axis.tvalid = src_valid;
assign s_axis.tlast = src_last;
assign s_axis.tid = '0;
assign s_axis.tdest = '0;
assign s_axis.tuser = src_frame ? user_b : user_a;

always_ff @(posedge clk) begin
    if (src_valid && s_axis.tready) begin
        src_valid <= 1'b0;
        if (src_last) begin
            src_beat <= '0;
            if (src_frame) src_done <= 1'b1;
            src_frame <= 1'b1;
        end else begin
            src_beat <= src_beat + 1;
        end
    end
    if ((!src_valid || s_axis.tready) && in_go && !src_done && !(src_valid && s_axis.tready && src_last && src_frame)) begin
        src_valid <= 1'b1;
    end
    if (rst) begin
        src_frame <= 1'b0;
        src_beat <= '0;
        src_done <= 1'b0;
        src_valid <= 1'b0;
    end
end

// ---------------------------------------------------------------- DUT

wire        desc_valid;
key_t       desc_key;
wire        desc_lookup, desc_fin, desc_rst;
wire [15:0] desc_len;
meta_t      desc_meta;

natgw_parser #(
    .LANE(LANE),
    .USER_W(2)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_axis(s_axis),
    .m_axis_hold(hold_axis),
    .m_desc_valid(desc_valid),
    .m_desc_ready(desc_ready),
    .m_desc_key(desc_key),
    .m_desc_lookup(desc_lookup),
    .m_desc_fin(desc_fin),
    .m_desc_rst(desc_rst),
    .m_desc_len(desc_len),
    .m_desc_meta(desc_meta),
    .cfg_bypass(bypass)
);

assign hold_axis.tready = hold_ready;

// ---------------------------------------------------------------- checks

logic       chk_frame = 1'b0;
logic [2:0] chk_beat = '0;
logic [1:0] desc_cnt = '0;

wire [6:0] chk_len = chk_frame ? len_b : len_a;

// P2.1 hold output reproduces the input
always_ff @(posedge clk) begin
    if (!rst && hold_axis.tvalid && hold_axis.tready) begin
        assert (hold_axis.tdata == (chk_frame ? beat_data(frame_b, int'(chk_beat)) : beat_data(frame_a, int'(chk_beat))));
        assert (hold_axis.tkeep == beat_keep(chk_len, int'(chk_beat)));
        assert (hold_axis.tlast == (16*(int'(chk_beat)+1) >= int'(chk_len)));
        assert (hold_axis.tuser == (chk_frame ? user_b : user_a));
        assert (!(chk_frame && chk_beat == 3'd7));
        if (hold_axis.tlast) begin
            chk_beat <= '0;
            chk_frame <= 1'b1;
        end else begin
            chk_beat <= chk_beat + 1;
        end
    end
    if (rst) begin
        chk_frame <= 1'b0;
        chk_beat <= '0;
    end
end

// P2.2 / P2.3 descriptors
always_ff @(posedge clk) begin
    if (!rst && desc_valid && desc_ready) begin
        assert (desc_cnt < 2'd2);
        if (desc_cnt == 2'd0) begin
            assert (desc_key == ref_a.key);
            assert (desc_lookup == ref_a.lookup);
            assert (desc_fin == ref_a.fin);
            assert (desc_rst == ref_a.rst);
            assert (desc_len == ref_a.len);
            assert (desc_meta == ref_a.meta);
        end else begin
            assert (desc_key == ref_b.key);
            assert (desc_lookup == ref_b.lookup);
            assert (desc_fin == ref_b.fin);
            assert (desc_rst == ref_b.rst);
            assert (desc_len == ref_b.len);
            assert (desc_meta == ref_b.meta);
        end
        desc_cnt <= desc_cnt + 1;
    end
    if (rst) desc_cnt <= '0;
end

// P2.4 stability while stalled
logic        p_desc_stall = 1'b0;
key_t        p_desc_key;
meta_t       p_desc_meta;
logic        p_hold_stall = 1'b0;
logic [127:0] p_hold_data;
logic        p_hold_last;

always_ff @(posedge clk) begin
    p_desc_stall <= !rst && desc_valid && !desc_ready;
    p_desc_key <= desc_key;
    p_desc_meta <= desc_meta;
    p_hold_stall <= !rst && hold_axis.tvalid && !hold_axis.tready;
    p_hold_data <= hold_axis.tdata;
    p_hold_last <= hold_axis.tlast;
    if (!rst && p_desc_stall) begin
        assert (desc_valid);
        assert (desc_key == p_desc_key);
        assert (desc_meta == p_desc_meta);
    end
    if (!rst && p_hold_stall) begin
        assert (hold_axis.tvalid);
        assert (hold_axis.tdata == p_hold_data);
        assert (hold_axis.tlast == p_hold_last);
    end
end

// covers: both descriptors are reachable
always_ff @(posedge clk) begin
    if (!rst) cover (desc_cnt == 2'd2);
end

endmodule
