// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: single-clock RAM, port A read-only, port B read/write

Written in a form Vivado infers as UltraRAM (RAM_STYLE = "ultra") or block RAM
(RAM_STYLE = "block"). Read latency is PIPE cycles on both ports (PIPE >= 2): one input register
stage, the memory read, then output stages absorbed into the URAM cascade.
A write takes effect one cycle after it is presented.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_ram #(
    parameter DATA_W = 72,
    parameter ADDR_W = 12,
    parameter PIPE = 3,
    /* verilator lint_off UNUSEDPARAM */
    parameter RAM_STYLE = "ultra",
    // longest URAM cascade Vivado may build; deeper memories are split into
    // several cascades joined by a mux that uses the extra PIPE stages
    parameter CASCADE_HEIGHT = 8
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

// all inputs are registered next to the memory (address fan-out to a deep
// URAM cascade is the critical path otherwise); total read latency stays PIPE
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

(* ram_style = RAM_STYLE, cascade_height = CASCADE_HEIGHT *)
logic [DATA_W-1:0] mem[2**ADDR_W];

logic [DATA_W-1:0] a_pipe[PIPE-1];
logic [DATA_W-1:0] b_pipe[PIPE-1];

always_ff @(posedge clk) begin
    if (a_en_reg) begin
        a_pipe[0] <= mem[a_addr_reg];
    end
    for (int i = 1; i < PIPE-1; i++) begin
        a_pipe[i] <= a_pipe[i-1];
    end
end

always_ff @(posedge clk) begin
    if (b_en_reg) begin
        if (b_we_reg) begin
            mem[b_addr_reg] <= b_din_reg;
        end else begin
            b_pipe[0] <= mem[b_addr_reg];
        end
    end
    for (int i = 1; i < PIPE-1; i++) begin
        b_pipe[i] <= b_pipe[i-1];
    end
end

assign a_dout = a_pipe[PIPE-2];
assign b_dout = b_pipe[PIPE-2];

endmodule

`resetall
