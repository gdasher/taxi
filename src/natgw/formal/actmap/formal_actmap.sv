// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_actmap (activity bitmap of the DDR tier)

Sets (any bit, every cycle if the environment chooses, but pausing as the
producer does while set_almost_full: up to SLACK more sets may arrive after it
rises), host read-and-clear of any word and table clears interleave freely.
One word W (any value) is tracked by a shadow that applies each operation in
the order natgw_actmap issues it.

  P10.1  a host read of W returns exactly the bits set in W since the last
         read or clear of W (read-modify-write forwarding is exact, including
         sets and reads of W in consecutive cycles)
  P10.2  host reads return in order, RAM_PIPE+1 cycles after issue
  P10.3  the set FIFO never overflows (no hit is lost)

*/

`default_nettype none

module formal_actmap #(
    parameter BIT_W = 8,
    parameter RAM_PIPE = 2,
    parameter SLACK = 6
)
(
    input  wire logic              clk,
    input  wire logic              set_valid,
    input  wire logic [BIT_W-1:0]  set_bit,
    input  wire logic              host_valid,
    input  wire logic [BIT_W-7:0]  host_word,
    input  wire logic              clear_start
);

localparam WORD_W = BIT_W - 6;

logic [WORD_W-1:0] W;
always_ff @(posedge clk) W <= W;      // arbitrary constant

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

wire host_ready, host_rvalid, clear_busy, almost_full, overflow;
wire [63:0] host_rdata;
wire [1:0] iop;
wire [WORD_W-1:0] iword;
wire [5:0] ibit;

natgw_actmap #(
    .BIT_W(BIT_W),
    .RAM_PIPE(RAM_PIPE),
    .SET_FIFO_DEPTH(16)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_set_valid(set_valid),
    .s_set_bit(set_bit),
    .host_valid(host_valid),
    .host_ready(host_ready),
    .host_word(host_word),
    .host_rvalid(host_rvalid),
    .host_rdata(host_rdata),
    .clear_start(clear_start),
    .clear_busy(clear_busy),
    .set_almost_full(almost_full),
    .overflow(overflow),
    .obs_issue_op(iop),
    .obs_issue_word(iword),
    .obs_issue_bit(ibit)
);

// the producer: after almost_full rises, at most SLACK more sets (its
// pipeline) until it falls again
logic [3:0] since_af = '0;
always_ff @(posedge clk) begin
    if (!almost_full) since_af <= '0;
    else if (set_valid && since_af != 4'hf) since_af <= since_af + 1;
end
always_comb if (!rst && almost_full && since_af >= 4'(SLACK)) assume (!set_valid);

// a host request is a single-cycle pulse, issued only when ready and not
// while a read is in flight (one register command at a time)
logic [RAM_PIPE+1:0] rd_inflight = '0;
always_ff @(posedge clk) rd_inflight <= {rd_inflight[RAM_PIPE:0], host_valid && host_ready};
always_comb if (!rst && host_valid) assume (host_ready && rd_inflight == '0);
always_comb if (!rst && clear_start) assume (!host_valid);

// ---------------------------------------------------------------- shadow of word W (issue order)
localparam OP_SET = 2'd1, OP_RC = 2'd2, OP_CLR = 2'd3;

logic [63:0] sh = '0;
logic sh_known = 1'b0;   // W cleared (by a clear or a read) since reset

always_ff @(posedge clk) begin
    if (!rst && iword == W) begin
        case (iop)
            OP_SET: sh <= sh | (64'd1 << ibit);
            OP_RC, OP_CLR: begin
                sh <= '0;
                sh_known <= 1'b1;
            end
            default: begin end
        endcase
    end
    if (rst) sh_known <= 1'b0;
end

localparam RLAT = RAM_PIPE + 1;
logic        rq_valid[RLAT];
logic        rq_trk[RLAT];
logic [63:0] rq_exp[RLAT];

always_ff @(posedge clk) begin
    rq_valid[0] <= !rst && iop == OP_RC;
    rq_trk[0] <= !rst && iop == OP_RC && iword == W && sh_known;
    rq_exp[0] <= sh;
    for (int i = 1; i < RLAT; i++) begin
        rq_valid[i] <= rq_valid[i-1];
        rq_trk[i] <= rq_trk[i-1];
        rq_exp[i] <= rq_exp[i-1];
    end
    if (rst) for (int i = 0; i < RLAT; i++) rq_valid[i] <= 1'b0;
end

always_comb begin
    if (!rst) begin
        // P10.2
        assert (host_rvalid == rq_valid[RLAT-1]);
        // P10.1
        if (host_rvalid && rq_trk[RLAT-1]) assert (host_rdata == rq_exp[RLAT-1]);
        // P10.3
        assert (!overflow);
    end
end

always_ff @(posedge clk) begin
    if (!rst) cover (host_rvalid && rq_trk[RLAT-1] && rq_exp[RLAT-1][0] && rq_exp[RLAT-1][63]);
    if (!rst) cover (almost_full);
end

endmodule
