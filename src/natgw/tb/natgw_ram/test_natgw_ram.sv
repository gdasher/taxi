// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: banked RAM testbench

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module test_natgw_ram #
(
    /* verilator lint_off WIDTHTRUNC */
    parameter DATA_W = 20,
    parameter ADDR_W = 8,
    parameter PIPE = 4,
    parameter BANK_AW = 2
    /* verilator lint_on WIDTHTRUNC */
)
();

logic clk;

logic a_en;
logic [ADDR_W-1:0] a_addr;
logic [DATA_W-1:0] a_dout;

logic b_en;
logic b_we;
logic [ADDR_W-1:0] b_addr;
logic [DATA_W-1:0] b_din;
logic [DATA_W-1:0] b_dout;

natgw_ram #(
    .DATA_W(DATA_W),
    .ADDR_W(ADDR_W),
    .PIPE(PIPE),
    .BANK_AW(BANK_AW),
    .RAM_STYLE("block")
)
uut (
    .clk(clk),
    .a_en(a_en),
    .a_addr(a_addr),
    .a_dout(a_dout),
    .b_en(b_en),
    .b_we(b_we),
    .b_addr(b_addr),
    .b_din(b_din),
    .b_dout(b_dout)
);

endmodule

`resetall
