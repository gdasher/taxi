// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: single-clock RAM, port A read-only, port B read/write

Written in a form Vivado infers as UltraRAM (RAM_STYLE = "ultra") or block RAM
(RAM_STYLE = "block"). Read latency is PIPE cycles on both ports (PIPE >= 1);
extra stages are absorbed into the URAM cascade output registers.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_ram #(
    parameter DATA_W = 72,
    parameter ADDR_W = 12,
    parameter PIPE = 3,
    /* verilator lint_off UNUSEDPARAM */
    parameter string RAM_STYLE = "ultra"
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

(* ram_style = RAM_STYLE *)
logic [DATA_W-1:0] mem[2**ADDR_W];

logic [DATA_W-1:0] a_pipe[PIPE];
logic [DATA_W-1:0] b_pipe[PIPE];

always_ff @(posedge clk) begin
    if (a_en) begin
        a_pipe[0] <= mem[a_addr];
    end
    for (int i = 1; i < PIPE; i++) begin
        a_pipe[i] <= a_pipe[i-1];
    end
end

always_ff @(posedge clk) begin
    if (b_en) begin
        if (b_we) begin
            mem[b_addr] <= b_din;
        end else begin
            b_pipe[0] <= mem[b_addr];
        end
    end
    for (int i = 1; i < PIPE; i++) begin
        b_pipe[i] <= b_pipe[i-1];
    end
end

assign a_dout = a_pipe[PIPE-1];
assign b_dout = b_pipe[PIPE-1];

endmodule

`resetall
