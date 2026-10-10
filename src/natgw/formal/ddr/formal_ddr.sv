// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_ddr (DDR tier of the flow table)

The memory is free: AXI read address and data channels stall arbitrarily and
return arbitrary data and response codes (errors included), but only for outstanding reads and in order per ID
(AXI's rule). Calibration and the host enable toggle at any time. Results
arrive on lanes 0 and 1 (any of them a DDR candidate or not), never more in
flight per lane than the shim's metadata FIFO allows (QUEUE_DEPTH). The
results of one lane T (0 or 1) are tracked through a shadow queue.

  P11.1  results leave in order per lane
  P11.2  a result that was not a DDR candidate (not looked up, or an on-chip
         hit) leaves unchanged; a candidate leaves unchanged (a miss) or as a
         hit carrying the DDR index flag, with its hash unchanged
  P11.3  per lane, never more than 2*MAX_OUT reads outstanding at the memory
  P11.4  no DDR lookup is issued while the tier is inactive, and every lane
         read belongs to an issued lookup (two reads each)
  P11.5  the per-lane queues never overflow

Host DDR access, the bulk clear and the activity bitmap's host port are idle
here (the bitmap has its own proof).

*/

`default_nettype none

module formal_ddr
    import natgw_pkg::*;
#(
    parameter DDR_BUCKET_W = 5,
    parameter MAX_OUT = 2,
    parameter QUEUE_DEPTH = 4
)
(
    input  wire logic               clk,

    input  wire logic               s_valid,
    input  wire logic               s_lane_bit,
    input  wire result_t            s_res,
    input  wire key_t               s_key,
    input  wire logic [31:0]        s_h1,
    input  wire logic               s_lookup,

    input  wire logic               arready,
    input  wire logic [3:0]         rid,
    input  wire logic [511:0]       rdata,
    input  wire logic [1:0]         rresp,     // free: error responses at any time
    input  wire logic               rvalid,

    input  wire logic               ddr_calib,
    input  wire logic               cfg_ddr_en,

    input  wire logic               nh_wr_valid,
    input  wire logic [NH_IDX_W-1:0] nh_wr_idx,
    input  wire nh_t                nh_wr_data
);

localparam AXI_ADDR_W = 12;

logic T;
always_ff @(posedge clk) T <= T;      // tracked lane, arbitrary constant

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

wire [LANE_W-1:0] s_lane = LANE_W'(s_lane_bit);

wire m_valid;
wire [LANE_W-1:0] m_lane;
result_t m_res;
wire [3:0] arid;
wire [AXI_ADDR_W-1:0] araddr;
wire arvalid;
wire ddr_active, stat_lookup, stat_hit, stat_skip, err_overflow;
wire rready, awvalid, wvalid, bready, host_ready, host_done, act_ready, act_rvalid, clear_busy;

natgw_ddr #(
    .DDR_BUCKET_W(DDR_BUCKET_W),
    .AXI_ADDR_W(AXI_ADDR_W),
    .AXI_ID_W(4),
    .MAX_OUT(MAX_OUT),
    .QUEUE_DEPTH(QUEUE_DEPTH),
    .NH_PIPE(2),
    .ACT_PIPE(2)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_valid(s_valid),
    .s_lane(s_lane),
    .s_res(s_res),
    .s_key(s_key),
    .s_h1(s_h1),
    .s_lookup(s_lookup),
    .m_valid(m_valid),
    .m_lane(m_lane),
    .m_res(m_res),
    .m_axi_awid(),
    .m_axi_awaddr(),
    .m_axi_awlen(),
    .m_axi_awsize(),
    .m_axi_awburst(),
    .m_axi_awvalid(awvalid),
    .m_axi_awready(1'b1),
    .m_axi_wdata(),
    .m_axi_wstrb(),
    .m_axi_wlast(),
    .m_axi_wvalid(wvalid),
    .m_axi_wready(1'b1),
    .m_axi_bid('0),
    .m_axi_bresp('0),
    .m_axi_bvalid(1'b0),
    .m_axi_bready(bready),
    .m_axi_arid(arid),
    .m_axi_araddr(araddr),
    .m_axi_arlen(),
    .m_axi_arsize(),
    .m_axi_arburst(),
    .m_axi_arvalid(arvalid),
    .m_axi_arready(arready),
    .m_axi_rid(rid),
    .m_axi_rdata(rdata),
    .m_axi_rresp(rresp),
    .m_axi_rlast(1'b1),
    .m_axi_rvalid(rvalid),
    .m_axi_rready(rready),
    .ddr_calib(ddr_calib),
    .cfg_ddr_en(cfg_ddr_en),
    .ddr_active(ddr_active),
    .clear_start(1'b0),
    .clear_busy(clear_busy),
    .host_valid(1'b0),
    .host_ready(host_ready),
    .host_op('0),
    .host_idx('0),
    .host_wdata('0),
    .host_done(host_done),
    .host_rdata(),
    .nh_wr_valid(nh_wr_valid),
    .nh_wr_idx(nh_wr_idx),
    .nh_wr_data(nh_wr_data),
    .nh_clear_start(1'b0),
    .act_valid(1'b0),
    .act_ready(act_ready),
    .act_word('0),
    .act_rvalid(act_rvalid),
    .act_rdata(),
    .stat_lookup(stat_lookup),
    .stat_hit(stat_hit),
    .stat_skip(stat_skip),
    .stat_rerr(),
    .err_overflow(err_overflow)
);

// ---------------------------------------------------------------- environment

// the memory: data only for outstanding reads, one beat per read
logic [3:0] rd_out[9];
always_ff @(posedge clk) begin
    for (int i = 0; i < 9; i++) begin
        rd_out[i] <= rd_out[i] + 4'(arvalid && arready && arid == 4'(i)) - 4'(rvalid && rid == 4'(i));
    end
    if (rst) for (int i = 0; i < 9; i++) rd_out[i] <= '0;
end
always_comb begin
    if (!rst && rvalid) assume (rid < 4'd9 && rd_out[rid] != 0 && !(arvalid && arready && arid == rid && rd_out[rid] == 0));
    if (rst) assume (!rvalid);
end

// in flight per lane (the shim's metadata FIFO bound)
logic [3:0] infl[2];
always_ff @(posedge clk) begin
    for (int l = 0; l < 2; l++) begin
        infl[l] <= infl[l] + 4'(s_valid && s_lane_bit == 1'(l)) - 4'(m_valid && m_lane == LANE_W'(l));
    end
    if (rst) for (int l = 0; l < 2; l++) infl[l] <= '0;
end
always_comb begin
    if (rst) assume (!s_valid);
    if (!rst && s_valid) assume (infl[s_lane_bit] < 4'(QUEUE_DEPTH));
end

// ---------------------------------------------------------------- shadow queue of lane T
localparam SQ = 8;
result_t sq_res[SQ];
logic    sq_cand[SQ];
logic [3:0] sq_wr = '0, sq_rd = '0;

wire in_t = !rst && s_valid && s_lane_bit == T;
wire out_t = !rst && m_valid && m_lane == LANE_W'(T);

always_ff @(posedge clk) begin
    if (in_t) begin
        sq_res[sq_wr[2:0]] <= s_res;
        sq_cand[sq_wr[2:0]] <= s_lookup && !s_res.hit;
        sq_wr <= sq_wr + 1;
    end
    if (out_t) sq_rd <= sq_rd + 1;
    if (rst) begin
        sq_wr <= '0;
        sq_rd <= '0;
    end
end

// stat_lookup is registered from the issue decision
logic past_active = 1'b0;
always_ff @(posedge clk) past_active <= ddr_active;

// lookups issued and lane reads made
logic [7:0] lookups = '0, lane_reads = '0;
always_ff @(posedge clk) begin
    lookups <= lookups + 8'(stat_lookup);
    lane_reads <= lane_reads + 8'(arvalid && arready && arid < 4'd8);
    if (rst) begin
        lookups <= '0;
        lane_reads <= '0;
    end
end

always_comb begin
    if (!rst) begin
        // P11.1, P11.2
        if (out_t) begin
            assert (sq_wr != sq_rd);
            if (!sq_cand[sq_rd[2:0]]) begin
                assert (m_res == sq_res[sq_rd[2:0]]);
            end else if (m_res.hit) begin
                assert (m_res.idx[31]);
                assert (m_res.hash == sq_res[sq_rd[2:0]].hash);
            end else begin
                assert (m_res == sq_res[sq_rd[2:0]]);
            end
        end
        // P11.3
        for (int l = 0; l < 8; l++) assert (rd_out[l] <= 4'(2 * MAX_OUT));
        // P11.4
        if (stat_lookup) assert (past_active);
        assert (lane_reads <= 8'(2 * lookups) + 8'd2);
        // P11.5
        assert (!err_overflow);
        // nothing on the write channels here
        assert (!awvalid && !wvalid);
    end
end

always_ff @(posedge clk) begin
    if (!rst) cover (out_t && m_res.hit && m_res.idx[31]);
    if (!rst) cover (stat_skip);
end

endmodule
