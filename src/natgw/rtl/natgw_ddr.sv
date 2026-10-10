// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: DDR tier of the flow table

Sits between the shared lookup engine (the UltraRAM tier) and the per-lane
result FIFOs. A frame that was looked up and missed on chip is looked up in
DDR when the tier is active (present, calibrated, enabled by the host and not
being cleared):

  * two-choice cuckoo table, two tables of 2**DDR_BUCKET_W buckets; a bucket is
    one 64-byte line holding two entries (slot 0 in bytes 0-31, slot 1 in bytes
    32-63); bucket b0 comes from the top bits of h0, b1 from the top bits of h1
  * the frame's two lines are read with AXI ID = its lane; AXI returns reads
    with one ID in order, so results stay in order per lane by construction
  * at most MAX_OUT DDR lookups are outstanding per lane; past that (or with
    the read request queue full) the frame continues as a miss and is punted
    to the host, as an unknown flow is today: DDR never stalls a lane
  * a DDR hit is reported with idx = 0x80000000 | DDR index ({table, bucket,
    slot}), takes its next hop from a mirror of the next-hop table, and sets
    its bit in the activity bitmap (no DDR write on the packet path)

Lines are compared as they arrive: each lane keeps the keys of its
outstanding lookups (at most MAX_OUT, in issue order, which is also AXI's
return order for the lane's ID), each returned line is compared against the
lane's oldest key, and only the outcome (hit, DDR index, action: about 90
bits) is kept per lane. No 512-bit line is buffered or muxed between lanes.

Every result, needing DDR or not, passes through a per-lane in-order queue, so
a hit or exception behind a pending DDR lookup waits for it. QUEUE_DEPTH must
cover every result a lane can have in flight: the shim's metadata FIFO bounds
that (a frame is looked up only once its metadata entry is queued); an
overflow is a configuration error and is flagged.

A read that returns an error (RRESP SLVERR or DECERR, e.g. an uncorrectable
ECC error) is not trusted: neither slot of that line can hit (the lookup's
other line still can: an entry lives in one place), a host read returns an
empty entry, and every such beat is counted.

The host writes, clears and reads DDR entries (writes are complete when their
write response returns), and a hardware engine zeroes the whole DDR table
(DDR contents are random after power-up). Host reads use AXI ID 8.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_ddr
    import natgw_pkg::*;
#(
    parameter DDR_BUCKET_W = 20,
    parameter AXI_ADDR_W = 34,
    parameter AXI_ID_W = 4,
    parameter logic [AXI_ADDR_W-1:0] DDR_BASE = '0,
    parameter MAX_OUT = 16,
    parameter QUEUE_DEPTH = 256,
    parameter NH_PIPE = 2,
    parameter ACT_PIPE = 4,          // the bitmap RAM: 4 covers up to 64 banks
    parameter DIDX_W = DDR_BUCKET_W + 2
)
(
    input  wire logic                   clk,
    input  wire logic                   rst,

    /*
     * Results from the lookup engine (no backpressure)
     */
    input  wire logic                   s_valid,
    input  wire logic [LANE_W-1:0]      s_lane,
    input  wire result_t                s_res,
    input  wire key_t                   s_key,
    input  wire logic [31:0]            s_h1,
    input  wire logic                   s_lookup,

    /*
     * Results to the per-lane FIFOs, in order per lane
     */
    output wire logic                   m_valid,
    output wire logic [LANE_W-1:0]      m_lane,
    output wire result_t                m_res,

    /*
     * AXI4 master to the memory controller (512-bit)
     */
    output wire logic [AXI_ID_W-1:0]    m_axi_awid,
    output wire logic [AXI_ADDR_W-1:0]  m_axi_awaddr,
    output wire logic [7:0]             m_axi_awlen,
    output wire logic [2:0]             m_axi_awsize,
    output wire logic [1:0]             m_axi_awburst,
    output wire logic                   m_axi_awvalid,
    input  wire logic                   m_axi_awready,
    output wire logic [511:0]           m_axi_wdata,
    output wire logic [63:0]            m_axi_wstrb,
    output wire logic                   m_axi_wlast,
    output wire logic                   m_axi_wvalid,
    input  wire logic                   m_axi_wready,
    input  wire logic [AXI_ID_W-1:0]    m_axi_bid,
    input  wire logic [1:0]             m_axi_bresp,
    input  wire logic                   m_axi_bvalid,
    output wire logic                   m_axi_bready,
    output wire logic [AXI_ID_W-1:0]    m_axi_arid,
    output wire logic [AXI_ADDR_W-1:0]  m_axi_araddr,
    output wire logic [7:0]             m_axi_arlen,
    output wire logic [2:0]             m_axi_arsize,
    output wire logic [1:0]             m_axi_arburst,
    output wire logic                   m_axi_arvalid,
    input  wire logic                   m_axi_arready,
    input  wire logic [AXI_ID_W-1:0]    m_axi_rid,
    input  wire logic [511:0]           m_axi_rdata,
    input  wire logic [1:0]             m_axi_rresp,
    input  wire logic                   m_axi_rlast,
    input  wire logic                   m_axi_rvalid,
    output wire logic                   m_axi_rready,

    /*
     * Status and control
     */
    input  wire logic                   ddr_calib,      // memory controller calibrated
    input  wire logic                   cfg_ddr_en,     // host enable
    output wire logic                   ddr_active,

    input  wire logic                   clear_start,    // zero the DDR table and the bitmap
    output wire logic                   clear_busy,

    /*
     * Host DDR entry access (one op at a time)
     */
    input  wire logic                   host_valid,
    output wire logic                   host_ready,
    input  wire logic [1:0]             host_op,        // 1 write, 2 clear, 3 read
    input  wire logic [DIDX_W-1:0]      host_idx,
    input  wire entry_t                 host_wdata,
    output wire logic                   host_done,      // write complete or read data valid
    output wire entry_t                 host_rdata,

    /*
     * Next-hop table mirror (host writes)
     */
    input  wire logic                   nh_wr_valid,
    input  wire logic [NH_IDX_W-1:0]    nh_wr_idx,
    input  wire nh_t                    nh_wr_data,
    input  wire logic                   nh_clear_start,

    /*
     * Activity bitmap, host read-and-clear
     */
    input  wire logic                   act_valid,
    output wire logic                   act_ready,
    input  wire logic [DIDX_W-7:0]      act_word,
    output wire logic                   act_rvalid,
    output wire logic [63:0]            act_rdata,

    /*
     * Statistics (one-cycle pulses)
     */
    output wire logic                   stat_lookup,
    output wire logic                   stat_hit,
    output wire logic                   stat_skip,
    output wire logic                   stat_rerr,      // a read returned an error
    output wire logic                   err_overflow
);

localparam LINE_W = DDR_BUCKET_W + 1;           // {table, bucket}
localparam OUT_W = $clog2(MAX_OUT + 1);
localparam PK_AW = $clog2(MAX_OUT);              // per-lane outstanding keys
localparam [AXI_ID_W-1:0] HOST_ID = AXI_ID_W'(8);

if (AXI_ID_W < 4)
    $fatal(0, "Error: natgw_ddr needs AXI_ID_W >= 4 (instance %m)");
if (DIDX_W < 7)
    $fatal(0, "Error: natgw_ddr needs at least 128 entries (instance %m)");
if (MAX_OUT != 2**PK_AW || MAX_OUT < 2)
    $fatal(0, "Error: natgw_ddr MAX_OUT must be a power of two (instance %m)");

function automatic logic [AXI_ADDR_W-1:0] line_addr(input logic [LINE_W-1:0] line);
    return DDR_BASE + (AXI_ADDR_W'(line) << 6);
endfunction

// ---------------------------------------------------------------------------
// activity: present, calibrated, enabled, not clearing

logic clear_busy_reg = 1'b0;
wire act_clear_busy;
assign ddr_active = ddr_calib && cfg_ddr_en && !clear_busy_reg && !act_clear_busy;
assign clear_busy = clear_busy_reg || act_clear_busy;

// ---------------------------------------------------------------------------
// enqueue: every result enters its lane's queue; misses issue DDR reads

typedef struct packed {
    logic                    ddr;    // waits for its DDR outcome
    result_t                 res;
} qe_t;

// an outstanding DDR lookup: what each returned line is compared against
typedef struct packed {
    key_t                    key;
    logic [DDR_BUCKET_W-1:0] b0;     // table 0 bucket (top of h0)
    logic [DDR_BUCKET_W-1:0] b1;     // table 1 bucket (top of h1)
} pk_t;

// the outcome of one DDR lookup
typedef struct packed {
    logic                    hit;
    logic [DIDX_W-1:0]       didx;   // {table, bucket, slot}
    logic                    xlate_dst;
    logic [31:0]             new_ip;
    logic [15:0]             new_port;
    logic                    dec_ttl;
    logic [NH_IDX_W-1:0]     nh_idx;
} dres_t;

localparam QE_W = $bits(qe_t);

logic [OUT_W-1:0] out_cnt_reg[LANES];
logic [LANES-1:0] out_dec;

// read request queue: one entry per DDR lookup (two lines)
typedef struct packed {
    logic [LANE_W-1:0] lane;
    logic [LINE_W-1:0] line0;
    logic [LINE_W-1:0] line1;
} arq_t;

wire arq_s_ready;
wire arq_m_valid;
arq_t arq_m_data;
logic arq_m_ready;

wire want_ddr = s_valid && s_lookup && !s_res.hit && ddr_active;
wire can_ddr = out_cnt_reg[s_lane] < OUT_W'(MAX_OUT) && arq_s_ready;
wire do_ddr = want_ddr && can_ddr;

arq_t arq_in;
always_comb begin
    arq_in.lane = s_lane;
    arq_in.line0 = {1'b0, s_res.hash[31 -: DDR_BUCKET_W]};
    arq_in.line1 = {1'b1, s_h1[31 -: DDR_BUCKET_W]};
end

natgw_fifo #(
    .DATA_W($bits(arq_t)),
    .DEPTH(16)
)
arq_inst (
    .clk(clk),
    .rst(rst),
    .s_valid(do_ddr),
    .s_ready(arq_s_ready),
    .s_data(arq_in),
    .m_valid(arq_m_valid),
    .m_ready(arq_m_ready),
    .m_data(arq_m_data),
    .overflow()
);

logic stat_lookup_reg = 1'b0, stat_skip_reg = 1'b0;
assign stat_lookup = stat_lookup_reg;
assign stat_skip = stat_skip_reg;

always_ff @(posedge clk) begin
    stat_lookup_reg <= do_ddr;
    stat_skip_reg <= want_ddr && !can_ddr;
    for (int l = 0; l < LANES; l++) begin
        out_cnt_reg[l] <= out_cnt_reg[l] + OUT_W'(do_ddr && s_lane == LANE_W'(l)) - OUT_W'(out_dec[l]);
    end
    if (rst) begin
        stat_lookup_reg <= 1'b0;
        stat_skip_reg <= 1'b0;
        for (int l = 0; l < LANES; l++) begin
            out_cnt_reg[l] <= '0;
        end
    end
end

qe_t q_in;
pk_t pk_in;
always_comb begin
    q_in.ddr = do_ddr;
    q_in.res = s_res;
    pk_in.key = s_key;
    pk_in.b0 = s_res.hash[31 -: DDR_BUCKET_W];
    pk_in.b1 = s_h1[31 -: DDR_BUCKET_W];
end

wire [LANES-1:0] q_m_valid;
qe_t q_m_data[LANES];
logic [LANES-1:0] q_m_ready;
wire [LANES-1:0] q_overflow;

// per lane: outstanding keys (read by the compare) and finished outcomes
logic [LANES-1:0] pk_pop;               // a lookup's second line was compared
pk_t pk_head[LANES];
logic [LANES-1:0] dr_push;
dres_t dr_in;
wire [LANES-1:0] dr_valid;
dres_t dr_data[LANES];
logic [LANES-1:0] dr_pop;

for (genvar l = 0; l < LANES; l = l + 1) begin : lane

    natgw_fifo #(
        .DATA_W(QE_W),
        .DEPTH(QUEUE_DEPTH)
    )
    q_inst (
        .clk(clk),
        .rst(rst),
        .s_valid(s_valid && s_lane == LANE_W'(l)),
        .s_ready(),
        .s_data(q_in),
        .m_valid(q_m_valid[l]),
        .m_ready(q_m_ready[l]),
        .m_data(q_m_data[l]),
        .overflow(q_overflow[l])
    );

    // keys of this lane's outstanding lookups, oldest first (at most MAX_OUT:
    // the issue cap); the compare reads the oldest, so AXI's in-order return
    // per ID pairs every line with its key
    pk_t pk_mem[MAX_OUT];
    logic [PK_AW-1:0] pk_wr_ptr_reg = '0;
    logic [PK_AW-1:0] pk_rd_ptr_reg = '0;

    always_ff @(posedge clk) begin
        if (do_ddr && s_lane == LANE_W'(l)) begin
            pk_mem[pk_wr_ptr_reg] <= pk_in;
            pk_wr_ptr_reg <= pk_wr_ptr_reg + 1;
        end
        if (pk_pop[l]) begin
            pk_rd_ptr_reg <= pk_rd_ptr_reg + 1;
        end
        if (rst) begin
            pk_wr_ptr_reg <= '0;
            pk_rd_ptr_reg <= '0;
        end
    end

    assign pk_head[l] = pk_mem[pk_rd_ptr_reg];

    // finished outcomes, in order (at most MAX_OUT: one per outstanding lookup)
    natgw_fifo #(
        .DATA_W($bits(dres_t)),
        .DEPTH(MAX_OUT)
    )
    dr_inst (
        .clk(clk),
        .rst(rst),
        .s_valid(dr_push[l]),
        .s_ready(),
        .s_data(dr_in),
        .m_valid(dr_valid[l]),
        .m_ready(dr_pop[l]),
        .m_data(dr_data[l]),
        .overflow()
    );

end

assign err_overflow = |q_overflow;

// ---------------------------------------------------------------------------
// AXI read address: host reads first, then lookups (two lines each)

logic ar_valid_reg = 1'b0;
logic [AXI_ID_W-1:0] ar_id_reg = '0;
logic [AXI_ADDR_W-1:0] ar_addr_reg = '0;
logic ar_second_reg = 1'b0;     // the current lookup's second line is next

logic host_ar_pend_reg = 1'b0;
logic [LINE_W-1:0] host_line_reg = '0;

assign m_axi_arid = ar_id_reg;
assign m_axi_araddr = ar_addr_reg;
assign m_axi_arlen = 8'd0;
assign m_axi_arsize = 3'd6;
assign m_axi_arburst = 2'b01;
assign m_axi_arvalid = ar_valid_reg;

always_comb begin
    arq_m_ready = 1'b0;
    if ((!ar_valid_reg || m_axi_arready) && !host_ar_pend_reg && ar_second_reg) begin
        arq_m_ready = 1'b1;
    end
end

always_ff @(posedge clk) begin
    if (ar_valid_reg && m_axi_arready) begin
        ar_valid_reg <= 1'b0;
    end
    if (!ar_valid_reg || m_axi_arready) begin
        if (host_ar_pend_reg) begin
            ar_valid_reg <= 1'b1;
            ar_id_reg <= HOST_ID;
            ar_addr_reg <= line_addr(host_line_reg);
            host_ar_pend_reg <= 1'b0;
        end else if (arq_m_valid) begin
            ar_valid_reg <= 1'b1;
            ar_id_reg <= AXI_ID_W'(arq_m_data.lane);
            ar_addr_reg <= line_addr(ar_second_reg ? arq_m_data.line1 : arq_m_data.line0);
            ar_second_reg <= !ar_second_reg;
        end
    end
    if (host_valid && host_ready && host_op == 2'd3) begin
        host_ar_pend_reg <= 1'b1;
        host_line_reg <= host_idx[DIDX_W-1:1];
    end
    if (rst) begin
        ar_valid_reg <= 1'b0;
        ar_second_reg <= 1'b0;
        host_ar_pend_reg <= 1'b0;
    end
end

// ---------------------------------------------------------------------------
// AXI read data: lane lines are compared on arrival, the host ID goes to the
// host. Every lane has room for all its outstanding outcomes, so RREADY is 1.

logic host_rd_done_reg = 1'b0;
entry_t host_rdata_reg = '0;
logic host_slot_reg = 1'b0;

assign m_axi_rready = 1'b1;

always_ff @(posedge clk) begin
    host_rd_done_reg <= 1'b0;
    if (m_axi_rvalid && m_axi_rid == HOST_ID) begin
        host_rd_done_reg <= 1'b1;
        host_rdata_reg <= entry_t'(host_slot_reg ? m_axi_rdata[256 +: ENTRY_W] : m_axi_rdata[0 +: ENTRY_W]);
        if (m_axi_rresp[1]) begin
            host_rdata_reg <= '0;
        end
    end
    if (host_valid && host_ready) begin
        host_slot_reg <= host_idx[0];
    end
    if (rst) begin
        host_rd_done_reg <= 1'b0;
    end
end

// compare on arrival
//   r0: the beat (lane IDs only), with the two entries it holds
//   r1: the lane's oldest key (read here, so a second line advances the key
//       before the next beat of that lane reaches this stage)
//   r2: compare both slots; on a lookup's second line, combine with the first
//       line's outcome (table 0 wins, slot 0 before slot 1) and push it

entry_t r0_slot0, r0_slot1;
logic r0_valid_reg = 1'b0;
logic [LANE_W-1:0] r0_lane_reg = '0;
logic r0_err_reg = 1'b0;
entry_t r0_slot0_reg = '0, r0_slot1_reg = '0;
logic stat_rerr_reg = 1'b0;

assign stat_rerr = stat_rerr_reg;

always_ff @(posedge clk) begin
    r0_valid_reg <= m_axi_rvalid && m_axi_rid < AXI_ID_W'(LANES);
    r0_lane_reg <= LANE_W'(m_axi_rid);
    r0_err_reg <= m_axi_rresp[1];      // SLVERR or DECERR: the line is not trusted
    stat_rerr_reg <= m_axi_rvalid && m_axi_rresp[1];
    r0_slot0_reg <= entry_t'(m_axi_rdata[0 +: ENTRY_W]);
    r0_slot1_reg <= entry_t'(m_axi_rdata[256 +: ENTRY_W]);
    if (rst) begin
        r0_valid_reg <= 1'b0;
        stat_rerr_reg <= 1'b0;
    end
end

// which line of its lookup each lane's next beat is
logic [LANES-1:0] second_reg = '0;

logic r1_valid_reg = 1'b0;
logic [LANE_W-1:0] r1_lane_reg = '0;
logic r1_second_reg = 1'b0;
logic r1_err_reg = 1'b0;
entry_t r1_slot0_reg = '0, r1_slot1_reg = '0;
pk_t r1_pk_reg = '0;

always_comb begin
    pk_pop = '0;
    for (int l = 0; l < LANES; l++) begin
        pk_pop[l] = r0_valid_reg && r0_lane_reg == LANE_W'(l) && second_reg[l];
    end
end

always_ff @(posedge clk) begin
    pk_t pk;
    pk = pk_head[0];
    for (int l = 0; l < LANES; l++) begin
        if (r0_lane_reg == LANE_W'(l)) begin
            pk = pk_head[l];
        end
    end
    r1_valid_reg <= r0_valid_reg;
    r1_lane_reg <= r0_lane_reg;
    r1_second_reg <= second_reg[r0_lane_reg];
    r1_err_reg <= r0_err_reg;
    r1_slot0_reg <= r0_slot0_reg;
    r1_slot1_reg <= r0_slot1_reg;
    r1_pk_reg <= pk;
    if (r0_valid_reg) begin
        second_reg[r0_lane_reg] <= !second_reg[r0_lane_reg];
    end
    if (rst) begin
        r1_valid_reg <= 1'b0;
        second_reg <= '0;
    end
end

// first-line outcomes waiting for their second line, per lane
dres_t first_reg[LANES];

always_comb begin
    logic h0, h1;
    entry_t e;
    dres_t d;
    h0 = !r1_err_reg && r1_slot0_reg.valid && r1_slot0_reg.key == r1_pk_reg.key;
    h1 = !r1_err_reg && r1_slot1_reg.valid && r1_slot1_reg.key == r1_pk_reg.key;
    e = h0 ? r1_slot0_reg : r1_slot1_reg;
    d.hit = h0 || h1;
    d.didx = {r1_second_reg, r1_second_reg ? r1_pk_reg.b1 : r1_pk_reg.b0, !h0};
    d.xlate_dst = e.xlate_dst;
    d.new_ip = e.new_ip;
    d.new_port = e.new_port;
    d.dec_ttl = e.dec_ttl;
    d.nh_idx = e.nh_idx;
    // a table-0 hit wins over table 1
    dr_in = first_reg[r1_lane_reg].hit ? first_reg[r1_lane_reg] : d;
    dr_push = '0;
    for (int l = 0; l < LANES; l++) begin
        dr_push[l] = r1_valid_reg && r1_second_reg && r1_lane_reg == LANE_W'(l);
    end
end

always_ff @(posedge clk) begin
    logic h0, h1;
    entry_t e;
    h0 = !r1_err_reg && r1_slot0_reg.valid && r1_slot0_reg.key == r1_pk_reg.key;
    h1 = !r1_err_reg && r1_slot1_reg.valid && r1_slot1_reg.key == r1_pk_reg.key;
    e = h0 ? r1_slot0_reg : r1_slot1_reg;
    if (r1_valid_reg && !r1_second_reg) begin
        first_reg[r1_lane_reg].hit <= h0 || h1;
        first_reg[r1_lane_reg].didx <= {1'b0, r1_pk_reg.b0, !h0};
        first_reg[r1_lane_reg].xlate_dst <= e.xlate_dst;
        first_reg[r1_lane_reg].new_ip <= e.new_ip;
        first_reg[r1_lane_reg].new_port <= e.new_port;
        first_reg[r1_lane_reg].dec_ttl <= e.dec_ttl;
        first_reg[r1_lane_reg].nh_idx <= e.nh_idx;
    end
    if (rst) begin
        for (int l = 0; l < LANES; l++) begin
            first_reg[l].hit <= 1'b0;
        end
    end
end

// ---------------------------------------------------------------------------
// merge: one lane per cycle, round robin among lanes whose head is ready

logic [LANES-1:0] lane_ready;
always_comb begin
    for (int l = 0; l < LANES; l++) begin
        lane_ready[l] = q_m_valid[l] && (!q_m_data[l].ddr || dr_valid[l]);
    end
end

logic [LANE_W-1:0] rr_reg = '0;
logic sel_valid;
logic [LANE_W-1:0] sel_lane;
wire act_almost_full;

// pause while the activity bitmap catches up (never drop a hit)
wire [LANES-1:0] lane_go = lane_ready & {LANES{!act_almost_full}};

// round robin: the first ready lane at or after rr_reg
always_comb begin
    // rotate so bit 0 is rr_reg, take the lowest set bit, rotate back
    logic [2*LANES-1:0] dbl;
    logic [LANES-1:0] rot;
    dbl = {lane_go, lane_go} >> rr_reg;
    rot = dbl[LANES-1:0];
    sel_lane = rr_reg;
    for (int i = LANES-1; i >= 0; i--) begin
        if (rot[i]) begin
            sel_lane = rr_reg + LANE_W'(i);
        end
    end
end

assign sel_valid = |lane_go;

// the selected lane's head and outcome (explicit compare and mux)
qe_t sel_qe;
dres_t sel_dr;

always_comb begin
    q_m_ready = '0;
    dr_pop = '0;
    out_dec = '0;
    sel_qe = q_m_data[0];
    sel_dr = dr_data[0];
    for (int l = 0; l < LANES; l++) begin
        if (sel_lane == LANE_W'(l)) begin
            sel_qe = q_m_data[l];
            sel_dr = dr_data[l];
            if (sel_valid) begin
                q_m_ready[l] = 1'b1;
                dr_pop[l] = q_m_data[l].ddr;
                out_dec[l] = q_m_data[l].ddr;
            end
        end
    end
end

// the selected result, with its DDR outcome applied
logic m2_valid_reg = 1'b0;
logic [LANE_W-1:0] m2_lane_reg = '0;
result_t m2_res_reg = '0;
logic m2_dhit_reg = 1'b0;
logic [DIDX_W-1:0] m2_didx_reg = '0;

always_ff @(posedge clk) begin
    logic hit;
    hit = sel_qe.ddr && sel_dr.hit;
    m2_valid_reg <= sel_valid;
    m2_lane_reg <= sel_lane;
    m2_res_reg <= sel_qe.res;
    m2_dhit_reg <= hit;
    m2_didx_reg <= sel_dr.didx;
    if (hit) begin
        m2_res_reg.hit <= 1'b1;
        m2_res_reg.xlate_dst <= sel_dr.xlate_dst;
        m2_res_reg.new_ip <= sel_dr.new_ip;
        m2_res_reg.new_port <= sel_dr.new_port;
        m2_res_reg.dec_ttl <= sel_dr.dec_ttl;
        m2_res_reg.nh_idx <= sel_dr.nh_idx;
    end
    if (sel_valid) begin
        rr_reg <= sel_lane + 1;
    end
    if (rst) begin
        m2_valid_reg <= 1'b0;
        rr_reg <= '0;
    end
end

// ---------------------------------------------------------------------------
// next hop for DDR hits

nh_t nh_dout;
logic nh_clear_busy_reg = 1'b0;
logic [NH_IDX_W-1:0] nh_clear_idx_reg = '0;

natgw_ram #(
    .DATA_W(NH_W),
    .ADDR_W(NH_IDX_W),
    .PIPE(NH_PIPE),
    .RAM_STYLE("block")
)
nh_ram_inst (
    .clk(clk),
    .a_en(1'b1),
    .a_addr(m2_res_reg.nh_idx),
    .a_dout(nh_dout),
    .b_en(nh_clear_busy_reg || nh_wr_valid),
    .b_we(1'b1),
    .b_addr(nh_clear_busy_reg ? nh_clear_idx_reg : nh_wr_idx),
    .b_din(nh_clear_busy_reg ? '0 : nh_wr_data),
    .b_dout()
);

always_ff @(posedge clk) begin
    if (nh_clear_busy_reg) begin
        nh_clear_idx_reg <= nh_clear_idx_reg + 1;
        if (&nh_clear_idx_reg) begin
            nh_clear_busy_reg <= 1'b0;
        end
    end
    if (nh_clear_start && !nh_clear_busy_reg) begin
        nh_clear_busy_reg <= 1'b1;
        nh_clear_idx_reg <= '0;
    end
    if (rst) begin
        nh_clear_busy_reg <= 1'b0;
    end
end

logic n_valid[NH_PIPE];
logic [LANE_W-1:0] n_lane[NH_PIPE];
result_t n_res[NH_PIPE];
logic n_dhit[NH_PIPE];
logic [DIDX_W-1:0] n_didx[NH_PIPE];

always_ff @(posedge clk) begin
    n_valid[0] <= m2_valid_reg;
    n_lane[0] <= m2_lane_reg;
    n_res[0] <= m2_res_reg;
    n_dhit[0] <= m2_dhit_reg;
    n_didx[0] <= m2_didx_reg;
    for (int i = 1; i < NH_PIPE; i++) begin
        n_valid[i] <= n_valid[i-1];
        n_lane[i] <= n_lane[i-1];
        n_res[i] <= n_res[i-1];
        n_dhit[i] <= n_dhit[i-1];
        n_didx[i] <= n_didx[i-1];
    end
    if (rst) begin
        for (int i = 0; i < NH_PIPE; i++) begin
            n_valid[i] <= 1'b0;
        end
    end
end

logic out_valid_reg = 1'b0;
logic [LANE_W-1:0] out_lane_reg = '0;
result_t out_res_reg = '0;
logic stat_hit_reg = 1'b0;
logic act_set_reg = 1'b0;
logic [DIDX_W-1:0] act_bit_reg = '0;

always_ff @(posedge clk) begin
    out_valid_reg <= n_valid[NH_PIPE-1];
    out_lane_reg <= n_lane[NH_PIPE-1];
    out_res_reg <= n_res[NH_PIPE-1];
    if (n_dhit[NH_PIPE-1]) begin
        out_res_reg.idx <= 32'h8000_0000 | 32'(n_didx[NH_PIPE-1]);
        out_res_reg.nh <= nh_dout;
    end
    stat_hit_reg <= n_valid[NH_PIPE-1] && n_dhit[NH_PIPE-1];
    act_set_reg <= n_valid[NH_PIPE-1] && n_dhit[NH_PIPE-1];
    act_bit_reg <= n_didx[NH_PIPE-1];
    if (rst) begin
        out_valid_reg <= 1'b0;
        stat_hit_reg <= 1'b0;
        act_set_reg <= 1'b0;
    end
end

assign m_valid = out_valid_reg;
assign m_lane = out_lane_reg;
assign m_res = out_res_reg;
assign stat_hit = stat_hit_reg;

// ---------------------------------------------------------------------------
// activity bitmap

natgw_actmap #(
    .BIT_W(DIDX_W),
    .RAM_PIPE(ACT_PIPE)
)
actmap_inst (
    .clk(clk),
    .rst(rst),
    .s_set_valid(act_set_reg),
    .s_set_bit(act_bit_reg),
    .host_valid(act_valid),
    .host_ready(act_ready),
    .host_word(act_word),
    .host_rvalid(act_rvalid),
    .host_rdata(act_rdata),
    .clear_start(clear_start),
    .clear_busy(act_clear_busy),
    .set_almost_full(act_almost_full),
    .overflow(),
    .obs_issue_op(),
    .obs_issue_word(),
    .obs_issue_bit()
);

// ---------------------------------------------------------------------------
// writes: host entry writes and clears (one slot, by byte strobes) and the
// bulk clear of every line

logic aw_valid_reg = 1'b0, w_valid_reg = 1'b0;
logic [AXI_ADDR_W-1:0] aw_addr_reg = '0;
logic [511:0] w_data_reg = '0;
logic [63:0] w_strb_reg = '0;
logic [LINE_W:0] b_pend_reg = '0;          // write responses outstanding
logic host_wr_busy_reg = 1'b0;
logic host_wr_done_reg = 1'b0;
logic [LINE_W:0] clear_line_reg = '0;      // next line to clear (MSB: done)

assign m_axi_awid = HOST_ID;
assign m_axi_awaddr = aw_addr_reg;
assign m_axi_awlen = 8'd0;
assign m_axi_awsize = 3'd6;
assign m_axi_awburst = 2'b01;
assign m_axi_awvalid = aw_valid_reg;
assign m_axi_wdata = w_data_reg;
assign m_axi_wstrb = w_strb_reg;
assign m_axi_wlast = 1'b1;
assign m_axi_wvalid = w_valid_reg;
assign m_axi_bready = 1'b1;

logic host_rd_busy_reg = 1'b0;

assign host_ready = !host_wr_busy_reg && !host_rd_busy_reg && !clear_busy_reg;
assign host_done = host_wr_done_reg || host_rd_done_reg;
assign host_rdata = host_rdata_reg;

wire aw_free = !aw_valid_reg || m_axi_awready;
wire w_free = !w_valid_reg || m_axi_wready;

always_ff @(posedge clk) begin
    logic b_inc, b_dec;
    b_inc = 1'b0;
    b_dec = m_axi_bvalid;
    host_wr_done_reg <= 1'b0;

    if (aw_valid_reg && m_axi_awready) begin
        aw_valid_reg <= 1'b0;
    end
    if (w_valid_reg && m_axi_wready) begin
        w_valid_reg <= 1'b0;
    end

    if (clear_busy_reg) begin
        if (!clear_line_reg[LINE_W]) begin
            if (aw_free && w_free) begin
                aw_valid_reg <= 1'b1;
                w_valid_reg <= 1'b1;
                aw_addr_reg <= line_addr(clear_line_reg[LINE_W-1:0]);
                w_data_reg <= '0;
                w_strb_reg <= '1;
                b_inc = 1'b1;
                clear_line_reg <= clear_line_reg + 1;
            end
        end else if (b_pend_reg == 0 && !aw_valid_reg && !w_valid_reg) begin
            clear_busy_reg <= 1'b0;
        end
    end else if (clear_start) begin
        clear_busy_reg <= 1'b1;
        clear_line_reg <= '0;
    end

    if (host_valid && host_ready && (host_op == 2'd1 || host_op == 2'd2)) begin
        // aw/w are free: no clear runs and the previous host write completed
        aw_valid_reg <= 1'b1;
        w_valid_reg <= 1'b1;
        aw_addr_reg <= line_addr(host_idx[DIDX_W-1:1]);
        w_data_reg <= host_idx[0] ? {256'(host_op == 2'd1 ? host_wdata : '0), 256'd0}
                                  : {256'd0, 256'(host_op == 2'd1 ? host_wdata : '0)};
        w_strb_reg <= host_idx[0] ? {32'hffffffff, 32'd0} : {32'd0, 32'hffffffff};
        b_inc = 1'b1;
        host_wr_busy_reg <= 1'b1;
    end
    // host writes run alone (no clear, one at a time): complete when its
    // address and data are accepted and its response has returned
    if (host_wr_busy_reg && !aw_valid_reg && !w_valid_reg && b_pend_reg == 0) begin
        host_wr_busy_reg <= 1'b0;
        host_wr_done_reg <= 1'b1;
    end
    if (host_valid && host_ready && host_op == 2'd3) begin
        host_rd_busy_reg <= 1'b1;
    end
    if (host_rd_done_reg) begin
        host_rd_busy_reg <= 1'b0;
    end

    b_pend_reg <= b_pend_reg + (LINE_W+1)'(b_inc) - (LINE_W+1)'(b_dec);

    if (rst) begin
        aw_valid_reg <= 1'b0;
        w_valid_reg <= 1'b0;
        b_pend_reg <= '0;
        host_wr_busy_reg <= 1'b0;
        host_wr_done_reg <= 1'b0;
        host_rd_busy_reg <= 1'b0;
        clear_busy_reg <= 1'b0;
    end
end

endmodule

`resetall
