// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: single-clock RAM, port A read-only, port B read/write

Behaves as one memory with read latency PIPE on both ports; a write takes
effect one cycle after it is presented (all inputs are registered next to the
memory).

Deep memories are built as up to 64 banks of 2**BANK_AW words (a short URAM
cascade each) joined by a mux tree with a register after every 8:1 level, so
no single cycle spans a long cascade:

    banks = 1:       PIPE >= 2
    banks = 2..8:    PIPE >= 3
    banks = 9..64:   PIPE >= 4

A spare stage is used first as a register on each bank output, then as output
registers. Written in a form Vivado infers as UltraRAM (RAM_STYLE = "ultra")
or block RAM (RAM_STYLE = "block").

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_ram #(
    parameter DATA_W = 72,
    parameter ADDR_W = 12,
    parameter PIPE = 3,
    // words per bank (log2); 13 = 8K words = two URAMs deep
    parameter BANK_AW = 13,
    /* verilator lint_off UNUSEDPARAM */
    parameter RAM_STYLE = "ultra"
    /* verilator lint_on UNUSEDPARAM */
)
(
    input  wire logic               clk,

    // port A: read
    input  wire logic               a_en,
    input  wire logic [ADDR_W-1:0]  a_addr,
    output wire logic [DATA_W-1:0]  a_dout,

    // port B: read or write
    input  wire logic               b_en,
    input  wire logic               b_we,
    input  wire logic [ADDR_W-1:0]  b_addr,
    input  wire logic [DATA_W-1:0]  b_din,
    output wire logic [DATA_W-1:0]  b_dout
);

localparam BAW = ADDR_W < BANK_AW ? ADDR_W : BANK_AW;     // address bits within a bank
localparam SEL_W = ADDR_W - BAW;                          // bank select bits
localparam SW = SEL_W > 0 ? SEL_W : 1;
localparam BANKS = 2**SEL_W;
localparam LEVELS = SEL_W == 0 ? 0 : (SEL_W <= 3 ? 1 : 2); // registered 8:1 mux levels
localparam MIN_PIPE = 2 + LEVELS;
localparam BANK_REG = PIPE > MIN_PIPE ? 1 : 0;
localparam OUT_REGS = PIPE - MIN_PIPE - BANK_REG;
localparam N1 = (BANKS + 7) / 8;                          // groups at the first level

if (SEL_W > 6)
    $fatal(0, "Error: natgw_ram supports at most 64 banks; raise BANK_AW (instance %m)");

if (PIPE < MIN_PIPE)
    $fatal(0, "Error: natgw_ram PIPE (%0d) must be at least %0d for %0d banks (instance %m)", PIPE, MIN_PIPE, BANKS);

// input registers
logic              a_en_reg = 1'b0;
logic [ADDR_W-1:0] a_addr_reg = '0;
logic              b_en_reg = 1'b0;
logic              b_we_reg = 1'b0;
logic [ADDR_W-1:0] b_addr_reg = '0;
logic [DATA_W-1:0] b_din_reg = '0;

always_ff @(posedge clk) begin
    a_en_reg <= a_en;
    a_addr_reg <= a_addr;
    b_en_reg <= b_en;
    b_we_reg <= b_we;
    b_addr_reg <= b_addr;
    b_din_reg <= b_din;
end

wire [SW-1:0] a_sel = SW'(a_addr_reg >> BAW);
wire [SW-1:0] b_sel = SW'(b_addr_reg >> BAW);

// banks
wire [DATA_W-1:0] a_bank[BANKS];
wire [DATA_W-1:0] b_bank[BANKS];

for (genvar k = 0; k < BANKS; k = k + 1) begin : bank

    (* ram_style = RAM_STYLE *)
    logic [DATA_W-1:0] mem[2**BAW];

    logic [DATA_W-1:0] a_q;
    logic [DATA_W-1:0] b_q;

    always_ff @(posedge clk) begin
        if (a_en_reg && (SEL_W == 0 || a_sel == SW'(k))) begin
            a_q <= mem[a_addr_reg[BAW-1:0]];
        end
    end

    always_ff @(posedge clk) begin
        if (b_en_reg && (SEL_W == 0 || b_sel == SW'(k))) begin
            if (b_we_reg) begin
                mem[b_addr_reg[BAW-1:0]] <= b_din_reg;
            end else begin
                b_q <= mem[b_addr_reg[BAW-1:0]];
            end
        end
    end

    if (BANK_REG != 0) begin : breg
        logic [DATA_W-1:0] a_r;
        logic [DATA_W-1:0] b_r;
        always_ff @(posedge clk) begin
            a_r <= a_q;
            b_r <= b_q;
        end
        assign a_bank[k] = a_r;
        assign b_bank[k] = b_r;
    end else begin : nbreg
        assign a_bank[k] = a_q;
        assign b_bank[k] = b_q;
    end

end

// bank select delayed to line up with the data: d0 with the bank output,
// d1 with the first mux level's output
logic [SW-1:0] a_sel_p = '0, b_sel_p = '0;     // aligned with a_q / b_q
wire  [SW-1:0] a_sel_d0, b_sel_d0;
logic [SW-1:0] a_sel_d1 = '0, b_sel_d1 = '0;

always_ff @(posedge clk) begin
    a_sel_p <= a_sel;
    b_sel_p <= b_sel;
    a_sel_d1 <= a_sel_d0;
    b_sel_d1 <= b_sel_d0;
end

if (BANK_REG != 0) begin : sreg
    logic [SW-1:0] a_r = '0, b_r = '0;
    always_ff @(posedge clk) begin
        a_r <= a_sel_p;
        b_r <= b_sel_p;
    end
    assign a_sel_d0 = a_r;
    assign b_sel_d0 = b_r;
end else begin : nsreg
    assign a_sel_d0 = a_sel_p;
    assign b_sel_d0 = b_sel_p;
end

// mux tree
wire [DATA_W-1:0] a_mux;
wire [DATA_W-1:0] b_mux;

if (LEVELS == 0) begin : m0

    assign a_mux = a_bank[0];
    assign b_mux = b_bank[0];

end else begin : mux

    // first level: groups of 8 banks, select bits [2:0]
    logic [DATA_W-1:0] a_l1[N1];
    logic [DATA_W-1:0] b_l1[N1];

    for (genvar g = 0; g < N1; g = g + 1) begin : l1
        wire [2:0] as = 3'(a_sel_d0);
        wire [2:0] bs = 3'(b_sel_d0);
        always_ff @(posedge clk) begin
            a_l1[g] <= a_bank[8*g + int'(as) < BANKS ? 8*g + int'(as) : 8*g];
            b_l1[g] <= b_bank[8*g + int'(bs) < BANKS ? 8*g + int'(bs) : 8*g];
        end
    end

    if (LEVELS == 1) begin : one
        assign a_mux = a_l1[0];
        assign b_mux = b_l1[0];
    end else begin : two
        // second level: select bits [5:3]
        logic [DATA_W-1:0] a_l2;
        logic [DATA_W-1:0] b_l2;
        wire [2:0] as = 3'(a_sel_d1 >> 3);
        wire [2:0] bs = 3'(b_sel_d1 >> 3);
        always_ff @(posedge clk) begin
            a_l2 <= a_l1[int'(as) < N1 ? int'(as) : 0];
            b_l2 <= b_l1[int'(bs) < N1 ? int'(bs) : 0];
        end
        assign a_mux = a_l2;
        assign b_mux = b_l2;
    end

end

// spare output stages
if (OUT_REGS == 0) begin : noreg
    assign a_dout = a_mux;
    assign b_dout = b_mux;
end else begin : oreg
    logic [DATA_W-1:0] a_o[OUT_REGS];
    logic [DATA_W-1:0] b_o[OUT_REGS];
    always_ff @(posedge clk) begin
        a_o[0] <= a_mux;
        b_o[0] <= b_mux;
        for (int i = 1; i < OUT_REGS; i++) begin
            a_o[i] <= a_o[i-1];
            b_o[i] <= b_o[i-1];
        end
    end
    assign a_dout = a_o[OUT_REGS-1];
    assign b_dout = b_o[OUT_REGS-1];
end

endmodule

`resetall
