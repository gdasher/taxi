// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: per-entry state (timestamps, counters, TCP flags),
aging scanner and event FIFO

One op enters the pipeline per cycle (hit > clear > host > scanner). Every op
reads its index on port A; the read returns RAM_PIPE cycles later, is
corrected by forwarding from the writes still in flight, and the op's write
(if any) is performed one cycle after that on port B.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_state
    import natgw_pkg::*;
#(
    parameter IDX_W = 19,
    parameter RAM_PIPE = 3,
    parameter EVT_DEPTH = 1024
)
(
    input  wire logic              clk,
    input  wire logic              rst,

    /*
     * Hits from the lookup engine (no backpressure)
     */
    input  wire logic              s_hit_valid,
    input  wire logic [IDX_W-1:0]  s_hit_idx,
    input  wire logic [15:0]       s_hit_len,
    input  wire logic              s_hit_fin,
    input  wire logic              s_hit_rst,

    output wire logic              bubble_req,

    /*
     * Host access
     */
    input  wire logic              host_st_valid,
    output wire logic              host_st_ready,
    input  wire logic              host_st_we,
    input  wire logic [IDX_W-1:0]  host_st_idx,
    input  wire state_t            host_st_wdata,
    output wire logic              host_st_rvalid,
    output wire state_t            host_st_rdata,

    /*
     * Events
     */
    output wire logic              m_evt_valid,
    input  wire logic              m_evt_ready,
    output wire logic [63:0]       m_evt,

    /*
     * Configuration
     */
    input  wire logic [31:0]       cfg_tick,
    input  wire logic [31:0]       cfg_thresh_tcp,
    input  wire logic [31:0]       cfg_thresh_udp,
    input  wire logic              cfg_scan_en,
    input  wire logic [15:0]       cfg_scan_interval,

    input  wire logic              clear_start,
    output wire logic              clear_busy,

    output wire logic              stat_evt_drop
);

localparam EVT_AW = $clog2(EVT_DEPTH);
// space the scanner keeps free in the event FIFO: 4 plus every op in flight
localparam SCAN_RESERVE = 4 + RAM_PIPE + 2;

typedef enum logic [2:0] {
    OP_NONE = 3'd0,
    OP_HIT  = 3'd1,
    OP_CLR  = 3'd2,
    OP_HWR  = 3'd3,
    OP_HRD  = 3'd4,
    OP_SCAN = 3'd5
} op_t;

// ---------------------------------------------------------------------------
// event FIFO (first-word fall-through via an output register)

logic [63:0] evt_mem[2**EVT_AW];
logic [EVT_AW:0] evt_wr_ptr_reg = '0;
logic [EVT_AW:0] evt_rd_ptr_reg = '0;
logic [63:0] evt_out_reg = '0;
logic evt_out_valid_reg = 1'b0;

wire [EVT_AW:0] evt_count = evt_wr_ptr_reg - evt_rd_ptr_reg;
wire evt_full = evt_count[EVT_AW];
wire evt_empty = evt_count == 0;
wire [EVT_AW:0] evt_free = (EVT_AW+1)'(2**EVT_AW) - evt_count;

logic evt_push;
logic [63:0] evt_push_data;

assign m_evt_valid = evt_out_valid_reg;
assign m_evt = evt_out_reg;

always_ff @(posedge clk) begin
    if (evt_push) begin
        evt_mem[evt_wr_ptr_reg[EVT_AW-1:0]] <= evt_push_data;
        evt_wr_ptr_reg <= evt_wr_ptr_reg + 1;
    end

    if ((!evt_out_valid_reg || m_evt_ready) && !evt_empty) begin
        evt_out_reg <= evt_mem[evt_rd_ptr_reg[EVT_AW-1:0]];
        evt_out_valid_reg <= 1'b1;
        evt_rd_ptr_reg <= evt_rd_ptr_reg + 1;
    end else if (m_evt_ready) begin
        evt_out_valid_reg <= 1'b0;
    end

    if (rst) begin
        evt_wr_ptr_reg <= '0;
        evt_rd_ptr_reg <= '0;
        evt_out_valid_reg <= 1'b0;
    end
end

// ---------------------------------------------------------------------------
// issue

logic clear_busy_reg = 1'b0;
logic [IDX_W-1:0] clear_idx_reg = '0;

logic [IDX_W-1:0] scan_idx_reg = '0;
logic [15:0] scan_cnt_reg = '0;

wire scan_due = cfg_scan_en && scan_cnt_reg == 0;
wire scan_room = evt_free >= (EVT_AW+1)'(SCAN_RESERVE);
wire scan_want = scan_due && scan_room;

op_t issue_op;
logic [IDX_W-1:0] issue_idx;

assign host_st_ready = !s_hit_valid && !clear_busy_reg;
assign clear_busy = clear_busy_reg;
assign bubble_req = scan_want || host_st_valid || clear_busy_reg;

always_comb begin
    issue_op = OP_NONE;
    issue_idx = s_hit_idx;
    if (s_hit_valid) begin
        issue_op = OP_HIT;
        issue_idx = s_hit_idx;
    end else if (clear_busy_reg) begin
        issue_op = OP_CLR;
        issue_idx = clear_idx_reg;
    end else if (host_st_valid) begin
        issue_op = host_st_we ? OP_HWR : OP_HRD;
        issue_idx = host_st_idx;
    end else if (scan_want) begin
        issue_op = OP_SCAN;
        issue_idx = scan_idx_reg;
    end
end

always_ff @(posedge clk) begin
    if (scan_cnt_reg != 0) begin
        scan_cnt_reg <= scan_cnt_reg - 1;
    end

    if (issue_op == OP_SCAN) begin
        scan_idx_reg <= scan_idx_reg + 1;
        scan_cnt_reg <= cfg_scan_interval == 0 ? 16'd0 : cfg_scan_interval - 1;
    end

    if (issue_op == OP_CLR) begin
        clear_idx_reg <= clear_idx_reg + 1;
        if (&clear_idx_reg) begin
            clear_busy_reg <= 1'b0;
        end
    end

    if (clear_start && !clear_busy_reg) begin
        clear_busy_reg <= 1'b1;
        clear_idx_reg <= '0;
    end

    if (rst) begin
        clear_busy_reg <= 1'b0;
        clear_idx_reg <= '0;
        scan_idx_reg <= '0;
        scan_cnt_reg <= '0;
    end
end

// ---------------------------------------------------------------------------
// RAM

state_t ram_a_dout;
logic w_en;
logic [IDX_W-1:0] w_idx_reg = '0;
state_t w_data_reg;

natgw_ram #(
    .DATA_W(STATE_W),
    .ADDR_W(IDX_W),
    .PIPE(RAM_PIPE),
    .RAM_STYLE("ultra")
)
ram_inst (
    .clk(clk),

    .a_en(issue_op != OP_NONE),
    .a_addr(issue_idx),
    .a_dout(ram_a_dout),

    .b_en(w_en),
    .b_we(1'b1),
    .b_addr(w_idx_reg),
    .b_din(w_data_reg),
    .b_dout()
);

// ---------------------------------------------------------------------------
// op pipeline (one stage per RAM read cycle)

op_t p_op_reg[RAM_PIPE];
logic [IDX_W-1:0] p_idx_reg[RAM_PIPE];
logic [15:0] p_len_reg[RAM_PIPE];
logic p_fin_reg[RAM_PIPE];
logic p_rst_reg[RAM_PIPE];
state_t p_wdata_reg[RAM_PIPE];

always_ff @(posedge clk) begin
    p_op_reg[0] <= issue_op;
    p_idx_reg[0] <= issue_idx;
    p_len_reg[0] <= s_hit_len;
    p_fin_reg[0] <= s_hit_fin;
    p_rst_reg[0] <= s_hit_rst;
    p_wdata_reg[0] <= host_st_wdata;
    for (int i = 1; i < RAM_PIPE; i++) begin
        p_op_reg[i] <= p_op_reg[i-1];
        p_idx_reg[i] <= p_idx_reg[i-1];
        p_len_reg[i] <= p_len_reg[i-1];
        p_fin_reg[i] <= p_fin_reg[i-1];
        p_rst_reg[i] <= p_rst_reg[i-1];
        p_wdata_reg[i] <= p_wdata_reg[i-1];
    end

    if (rst) begin
        for (int i = 0; i < RAM_PIPE; i++) begin
            p_op_reg[i] <= OP_NONE;
        end
    end
end

// ---------------------------------------------------------------------------
// write stage registers and write history (for forwarding)

op_t w_op_reg = OP_NONE;
logic w_we_reg = 1'b0;          // write requested (scan writes are gated by FIFO space)
logic [3:0] w_evt_reg = '0;     // event type to queue (0 = none)

// effective write performed this cycle
wire w_evt_ok = !evt_full;
assign w_en = w_we_reg && (w_op_reg != OP_SCAN || w_evt_ok);

logic h_valid_reg[1:RAM_PIPE+1];
logic [IDX_W-1:0] h_idx_reg[1:RAM_PIPE+1];
state_t h_data_reg[1:RAM_PIPE+1];

// forwarding: most recent write wins
state_t cur;

always_comb begin
    cur = ram_a_dout;
    for (int k = RAM_PIPE+1; k >= 1; k--) begin
        if (h_valid_reg[k] && h_idx_reg[k] == p_idx_reg[RAM_PIPE-1]) begin
            cur = h_data_reg[k];
        end
    end
    if (w_en && w_idx_reg == p_idx_reg[RAM_PIPE-1]) begin
        cur = w_data_reg;
    end
end

// compute
op_t c_op;
logic [IDX_W-1:0] c_idx;
state_t c_new;
logic c_we;
logic [3:0] c_evt;

always_comb begin
    c_op = p_op_reg[RAM_PIPE-1];
    c_idx = p_idx_reg[RAM_PIPE-1];
    c_new = cur;
    c_we = 1'b0;
    c_evt = 4'd0;

    case (c_op)
        OP_HIT: begin
            c_new.ts = cfg_tick;
            c_new.pkts = cur.pkts + 1;
            c_new.bytes = cur.bytes + 56'(p_len_reg[RAM_PIPE-1]);
            c_new.evp = 1'b0;
            c_new.fin = cur.fin | p_fin_reg[RAM_PIPE-1];
            c_new.rst = cur.rst | p_rst_reg[RAM_PIPE-1];
            c_we = 1'b1;
            if (p_fin_reg[RAM_PIPE-1]) begin
                c_evt = EVT_FIN;
            end else if (p_rst_reg[RAM_PIPE-1]) begin
                c_evt = EVT_RST;
            end
        end
        OP_CLR: begin
            c_new = '0;
            c_we = 1'b1;
        end
        OP_HWR: begin
            c_new = p_wdata_reg[RAM_PIPE-1];
            c_we = 1'b1;
        end
        OP_SCAN: begin
            if (cur.valid && !cur.evp && (cfg_tick - cur.ts) > (cur.tcp ? cfg_thresh_tcp : cfg_thresh_udp)) begin
                c_new.evp = 1'b1;
                c_we = 1'b1;
                c_evt = EVT_IDLE;
            end
        end
        default: begin end
    endcase
end

logic host_rvalid_reg = 1'b0;
state_t host_rdata_reg;

assign host_st_rvalid = host_rvalid_reg;
assign host_st_rdata = host_rdata_reg;

always_ff @(posedge clk) begin
    w_op_reg <= c_op;
    w_idx_reg <= c_idx;
    w_data_reg <= c_new;
    w_we_reg <= c_we;
    w_evt_reg <= c_evt;

    host_rvalid_reg <= c_op == OP_HRD;
    host_rdata_reg <= cur;

    h_valid_reg[1] <= w_en;
    h_idx_reg[1] <= w_idx_reg;
    h_data_reg[1] <= w_data_reg;
    for (int k = 2; k <= RAM_PIPE+1; k++) begin
        h_valid_reg[k] <= h_valid_reg[k-1];
        h_idx_reg[k] <= h_idx_reg[k-1];
        h_data_reg[k] <= h_data_reg[k-1];
    end

    if (rst) begin
        w_op_reg <= OP_NONE;
        w_we_reg <= 1'b0;
        w_evt_reg <= '0;
        host_rvalid_reg <= 1'b0;
        for (int k = 1; k <= RAM_PIPE+1; k++) begin
            h_valid_reg[k] <= 1'b0;
        end
    end
end

// ---------------------------------------------------------------------------
// event push

logic ovf_pend_reg = 1'b0;
logic evt_drop_reg = 1'b0;

assign stat_evt_drop = evt_drop_reg;

always_comb begin
    evt_push = 1'b0;
    evt_push_data = {w_evt_reg, 4'd0, 24'(w_idx_reg), cfg_tick};
    if (w_evt_reg != 0) begin
        evt_push = w_evt_ok;
    end else if (ovf_pend_reg && !evt_full) begin
        evt_push = 1'b1;
        evt_push_data = {EVT_OVF, 28'd0, cfg_tick};
    end
end

always_ff @(posedge clk) begin
    evt_drop_reg <= 1'b0;

    if (w_evt_reg != 0 && !w_evt_ok && w_op_reg == OP_HIT) begin
        // lost FIN/RST event (a scan that cannot queue its event simply does not set evp)
        evt_drop_reg <= 1'b1;
        ovf_pend_reg <= 1'b1;
    end else if (w_evt_reg == 0 && ovf_pend_reg && !evt_full) begin
        ovf_pend_reg <= 1'b0;
    end

    if (rst) begin
        ovf_pend_reg <= 1'b0;
        evt_drop_reg <= 1'b0;
    end
end

endmodule

`resetall
