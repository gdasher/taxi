// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: activity bitmap for the DDR tier

One bit per DDR-tier entry, set by every hit on that entry and read-and-cleared
by the host a 64-bit word at a time. The host learns which DDR flows were
active since its last scan without the packet path writing to DDR.

Words are updated read-modify-write: an op reads its word on port A, the read
returns RAM_PIPE cycles later, is corrected by forwarding from the writes still
in flight (the most recent wins), and the new value is written one cycle after
that on port B. One op per cycle: clear > host > hit. Hits wait in a small FIFO
while the host or the clear has the pipeline; set_almost_full asks the
producer to pause before the FIFO could overflow (a hit is never dropped).

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_actmap #(
    // bits = 2**BIT_W (at least 64)
    parameter BIT_W = 12,
    parameter RAM_PIPE = 4,
    parameter SET_FIFO_DEPTH = 32
)
(
    input  wire logic              clk,
    input  wire logic              rst,

    // set a bit (a hit); no backpressure
    input  wire logic              s_set_valid,
    input  wire logic [BIT_W-1:0]  s_set_bit,

    // host read-and-clear of one word
    input  wire logic              host_valid,
    output wire logic              host_ready,
    input  wire logic [BIT_W-7:0]  host_word,
    output wire logic              host_rvalid,
    output wire logic [63:0]       host_rdata,

    // clear every word
    input  wire logic              clear_start,
    output wire logic              clear_busy,

    output wire logic              set_almost_full,
    output wire logic              overflow,

    // observation of the issue order (verification only; removed by synthesis)
    output wire logic [1:0]        obs_issue_op,
    output wire logic [BIT_W-7:0]  obs_issue_word,
    output wire logic [5:0]        obs_issue_bit
);

localparam WORD_W = BIT_W - 6;

if (BIT_W < 6)
    $fatal(0, "Error: natgw_actmap BIT_W must be at least 6 (instance %m)");
if (RAM_PIPE < 2)
    $fatal(0, "Error: natgw_actmap RAM_PIPE must be at least 2 (instance %m)");

typedef enum logic [1:0] {
    OP_NONE = 2'd0,
    OP_SET = 2'd1,
    OP_RC = 2'd2,     // read and clear (host)
    OP_CLR = 2'd3
} op_t;

// pending hits
wire              set_q_valid;
wire [BIT_W-1:0]  set_q_bit;
logic             set_q_ready;

// occupancy (the FIFO's own count plus its output register), for the
// almost-full request: the producer stops with ALMOST_MARGIN entries to spare
localparam ALMOST_MARGIN = 8;
logic [$clog2(SET_FIFO_DEPTH+2):0] set_cnt_reg = '0;
assign set_almost_full = set_cnt_reg >= ($clog2(SET_FIFO_DEPTH+2)+1)'(SET_FIFO_DEPTH - ALMOST_MARGIN);

always_ff @(posedge clk) begin
    set_cnt_reg <= set_cnt_reg + ($clog2(SET_FIFO_DEPTH+2)+1)'(s_set_valid && !overflow)
        - ($clog2(SET_FIFO_DEPTH+2)+1)'(set_q_valid && set_q_ready);
    if (rst) begin
        set_cnt_reg <= '0;
    end
end

natgw_fifo #(
    .DATA_W(BIT_W),
    .DEPTH(SET_FIFO_DEPTH)
)
set_fifo_inst (
    .clk(clk),
    .rst(rst),
    .s_valid(s_set_valid),
    .s_ready(),
    .s_data(s_set_bit),
    .m_valid(set_q_valid),
    .m_ready(set_q_ready),
    .m_data(set_q_bit),
    .overflow(overflow)
);

// issue
logic clear_busy_reg = 1'b0;
logic [WORD_W-1:0] clear_idx_reg = '0;

op_t issue_op;
logic [WORD_W-1:0] issue_word;
logic [5:0] issue_bit;

assign clear_busy = clear_busy_reg;
assign host_ready = !clear_busy_reg;
assign obs_issue_op = issue_op;
assign obs_issue_word = issue_word;
assign obs_issue_bit = issue_bit;

always_comb begin
    issue_op = OP_NONE;
    issue_word = set_q_bit[BIT_W-1:6];
    issue_bit = set_q_bit[5:0];
    set_q_ready = 1'b0;
    if (clear_busy_reg) begin
        issue_op = OP_CLR;
        issue_word = clear_idx_reg;
    end else if (host_valid) begin
        issue_op = OP_RC;
        issue_word = host_word;
    end else if (set_q_valid) begin
        issue_op = OP_SET;
        set_q_ready = 1'b1;
    end
end

always_ff @(posedge clk) begin
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
    end
end

// RAM
logic [63:0] ram_a_dout;
logic w_en_reg = 1'b0;
logic [WORD_W-1:0] w_word_reg = '0;
logic [63:0] w_data_reg = '0;

natgw_ram #(
    .DATA_W(64),
    .ADDR_W(WORD_W),
    .PIPE(RAM_PIPE),
    .RAM_STYLE("ultra")
)
ram_inst (
    .clk(clk),
    .a_en(issue_op != OP_NONE),
    .a_addr(issue_word),
    .a_dout(ram_a_dout),
    .b_en(w_en_reg),
    .b_we(1'b1),
    .b_addr(w_word_reg),
    .b_din(w_data_reg),
    .b_dout()
);

// op pipeline, one stage per RAM read cycle
op_t p_op_reg[RAM_PIPE];
logic [WORD_W-1:0] p_word_reg[RAM_PIPE];
logic [5:0] p_bit_reg[RAM_PIPE];

always_ff @(posedge clk) begin
    p_op_reg[0] <= issue_op;
    p_word_reg[0] <= issue_word;
    p_bit_reg[0] <= issue_bit;
    for (int i = 1; i < RAM_PIPE; i++) begin
        p_op_reg[i] <= p_op_reg[i-1];
        p_word_reg[i] <= p_word_reg[i-1];
        p_bit_reg[i] <= p_bit_reg[i-1];
    end
    if (rst) begin
        for (int i = 0; i < RAM_PIPE; i++) begin
            p_op_reg[i] <= OP_NONE;
        end
    end
end

// write history: every write still able to race a read in the pipeline
logic h_valid_reg[1:RAM_PIPE+1];
logic [WORD_W-1:0] h_word_reg[1:RAM_PIPE+1];
logic [63:0] h_data_reg[1:RAM_PIPE+1];

logic [63:0] cur;

always_comb begin
    cur = ram_a_dout;
    for (int k = RAM_PIPE+1; k >= 1; k--) begin
        if (h_valid_reg[k] && h_word_reg[k] == p_word_reg[RAM_PIPE-1]) begin
            cur = h_data_reg[k];
        end
    end
    if (w_en_reg && w_word_reg == p_word_reg[RAM_PIPE-1]) begin
        cur = w_data_reg;
    end
end

logic host_rvalid_reg = 1'b0;
logic [63:0] host_rdata_reg = '0;

assign host_rvalid = host_rvalid_reg;
assign host_rdata = host_rdata_reg;

always_ff @(posedge clk) begin
    op_t op;
    op = p_op_reg[RAM_PIPE-1];

    w_en_reg <= op != OP_NONE;
    w_word_reg <= p_word_reg[RAM_PIPE-1];
    case (op)
        OP_SET: w_data_reg <= cur | (64'd1 << p_bit_reg[RAM_PIPE-1]);
        default: w_data_reg <= '0;   // read-and-clear, clear
    endcase

    host_rvalid_reg <= op == OP_RC;
    host_rdata_reg <= cur;

    h_valid_reg[1] <= w_en_reg;
    h_word_reg[1] <= w_word_reg;
    h_data_reg[1] <= w_data_reg;
    for (int k = 2; k <= RAM_PIPE+1; k++) begin
        h_valid_reg[k] <= h_valid_reg[k-1];
        h_word_reg[k] <= h_word_reg[k-1];
        h_data_reg[k] <= h_data_reg[k-1];
    end

    if (rst) begin
        w_en_reg <= 1'b0;
        host_rvalid_reg <= 1'b0;
        for (int k = 1; k <= RAM_PIPE+1; k++) begin
            h_valid_reg[k] <= 1'b0;
        end
    end
end

endmodule

`resetall
