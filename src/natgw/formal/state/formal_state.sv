// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_state

Hits (any index, any length, FIN/RST flags, every cycle if the environment
chooses), host writes and reads, the aging scanner and event backpressure all
interleave freely. One index I (any value) is tracked by a shadow that applies
each operation in the order natgw_state issues it.

  P5.1  a host read of I returns exactly the shadow's valid, tcp, fin, rst,
        packet and byte counts (read-modify-write forwarding is exact, including
        hits on I in consecutive cycles)
  P5.2  host operations are never accepted in a cycle with a hit (issue order
        is hit first), and host reads return in order, RAM_PIPE+1 cycles later
  P5.3  every event names a valid type and an index inside the table

*/

`default_nettype none

module formal_state
    import natgw_pkg::*;
#(
    parameter IDX_W = 3,
    parameter RAM_PIPE = 2,
    parameter EVT_DEPTH = 8
)
(
    input  wire logic             clk,

    input  wire logic             hit_valid,
    input  wire logic [IDX_W-1:0] hit_idx,
    input  wire logic [15:0]      hit_len,
    input  wire logic             hit_fin,
    input  wire logic             hit_rst,

    input  wire logic             st_valid,
    input  wire logic             st_we,
    input  wire logic [IDX_W-1:0] st_idx,
    input  wire state_t           st_wdata,

    input  wire logic             evt_ready,
    input  wire logic [31:0]      tick
);

logic [IDX_W-1:0] I;
logic [31:0] thresh_tcp;
logic [31:0] thresh_udp;
logic        scan_en;
logic [15:0] scan_interval;

// arbitrary constants: no initial value, held for the whole trace
// (the slang frontend does not honour (* anyconst *))
always_ff @(posedge clk) begin
    I <= I;
    thresh_tcp <= thresh_tcp;
    thresh_udp <= thresh_udp;
    scan_en <= scan_en;
    scan_interval <= scan_interval;
end

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

wire        st_ready, st_rvalid, evt_valid, bubble_req, clear_busy, evt_drop;
state_t     st_rdata;
wire [63:0] evt;

natgw_state #(
    .IDX_W(IDX_W),
    .RAM_PIPE(RAM_PIPE),
    .EVT_DEPTH(EVT_DEPTH)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_hit_valid(hit_valid),
    .s_hit_idx(hit_idx),
    .s_hit_len(hit_len),
    .s_hit_fin(hit_fin),
    .s_hit_rst(hit_rst),
    .bubble_req(bubble_req),
    .host_st_valid(st_valid),
    .host_st_ready(st_ready),
    .host_st_we(st_we),
    .host_st_idx(st_idx),
    .host_st_wdata(st_wdata),
    .host_st_rvalid(st_rvalid),
    .host_st_rdata(st_rdata),
    .m_evt_valid(evt_valid),
    .m_evt_ready(evt_ready),
    .m_evt(evt),
    .cfg_tick(tick),
    .cfg_thresh_tcp(thresh_tcp),
    .cfg_thresh_udp(thresh_udp),
    .cfg_scan_en(scan_en),
    .cfg_scan_interval(scan_interval),
    .clear_start(1'b0),
    .clear_busy(clear_busy),
    .stat_evt_drop(evt_drop)
);

// host request stays until accepted
logic p_st_valid, p_st_ready, p_st_we;
logic [IDX_W-1:0] p_st_idx;
state_t p_st_wdata;
always_ff @(posedge clk) begin
    p_st_valid <= st_valid;
    p_st_ready <= st_ready;
    p_st_we <= st_we;
    p_st_idx <= st_idx;
    p_st_wdata <= st_wdata;
end
always_comb begin
    if (!rst && p_st_valid && !p_st_ready) begin
        assume (st_valid && st_we == p_st_we && st_idx == p_st_idx && st_wdata == p_st_wdata);
    end
end

// ---------------------------------------------------------------- shadow of index I

logic   sh_known = 1'b0;
state_t sh;

wire host_acc = !rst && st_valid && st_ready;

always_ff @(posedge clk) begin
    if (!rst && hit_valid && hit_idx == I) begin
        sh.pkts <= sh.pkts + 1;
        sh.bytes <= sh.bytes + 56'(hit_len);
        sh.fin <= sh.fin | hit_fin;
        sh.rst <= sh.rst | hit_rst;
    end else if (host_acc && st_we && st_idx == I) begin
        sh <= st_wdata;
        sh_known <= 1'b1;
    end
    if (rst) sh_known <= 1'b0;
end

// P5.2 issue order
always_comb if (!rst) assert (!(host_acc && hit_valid));

// reads in flight
localparam RLAT = RAM_PIPE + 1;
logic   rq_valid[RLAT];
logic   rq_trk[RLAT];
state_t rq_exp[RLAT];

always_ff @(posedge clk) begin
    rq_valid[0] <= host_acc && !st_we;
    rq_trk[0] <= host_acc && !st_we && st_idx == I && sh_known;
    rq_exp[0] <= sh;
    for (int i = 1; i < RLAT; i++) begin
        rq_valid[i] <= rq_valid[i-1];
        rq_trk[i] <= rq_trk[i-1];
        rq_exp[i] <= rq_exp[i-1];
    end
    if (rst) for (int i = 0; i < RLAT; i++) rq_valid[i] <= 1'b0;
end

// P5.1 / P5.2
always_comb begin
    if (!rst) begin
        assert (st_rvalid == rq_valid[RLAT-1]);
        if (st_rvalid && rq_trk[RLAT-1]) begin
            assert (st_rdata.valid == rq_exp[RLAT-1].valid);
            assert (st_rdata.tcp == rq_exp[RLAT-1].tcp);
            assert (st_rdata.fin == rq_exp[RLAT-1].fin);
            assert (st_rdata.rst == rq_exp[RLAT-1].rst);
            assert (st_rdata.pkts == rq_exp[RLAT-1].pkts);
            assert (st_rdata.bytes == rq_exp[RLAT-1].bytes);
        end
    end
end

// P5.3 events
always_comb begin
    if (!rst && evt_valid) begin
        assert (evt[63:60] == EVT_IDLE || evt[63:60] == EVT_FIN || evt[63:60] == EVT_RST || evt[63:60] == EVT_OVF);
        if (evt[63:60] != EVT_OVF) assert (evt[55:32] < 24'(2**IDX_W));
    end
end

always_ff @(posedge clk) begin
    if (!rst) cover (st_rvalid && rq_trk[RLAT-1] && rq_exp[RLAT-1].pkts >= 48'd3);
end

endmodule
