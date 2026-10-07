// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: small synchronous FIFO for descriptors (distributed RAM)

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_fifo #(
    parameter DATA_W = 32,
    parameter DEPTH = 32
)
(
    input  wire logic               clk,
    input  wire logic               rst,

    input  wire logic               s_valid,
    output wire logic               s_ready,
    input  wire logic [DATA_W-1:0]  s_data,

    output wire logic               m_valid,
    input  wire logic               m_ready,
    output wire logic [DATA_W-1:0]  m_data,

    output wire logic               overflow
);

localparam AW = $clog2(DEPTH);

(* ram_style = "distributed" *)
logic [DATA_W-1:0] mem[2**AW];

logic [AW:0] wr_ptr_reg = '0;
logic [AW:0] rd_ptr_reg = '0;

wire full = wr_ptr_reg == (rd_ptr_reg ^ {1'b1, {AW{1'b0}}});
wire empty = wr_ptr_reg == rd_ptr_reg;

// registered output stage
logic              out_valid_reg = 1'b0;
logic [DATA_W-1:0] out_data_reg = '0;

wire pop = !empty && (!out_valid_reg || m_ready);

assign s_ready = !full;
assign m_valid = out_valid_reg;
assign m_data = out_data_reg;
assign overflow = s_valid && full;

always_ff @(posedge clk) begin
    if (s_valid && !full) begin
        mem[wr_ptr_reg[AW-1:0]] <= s_data;
        wr_ptr_reg <= wr_ptr_reg + 1;
    end

    if (m_ready) begin
        out_valid_reg <= 1'b0;
    end
    if (pop) begin
        out_data_reg <= mem[rd_ptr_reg[AW-1:0]];
        out_valid_reg <= 1'b1;
        rd_ptr_reg <= rd_ptr_reg + 1;
    end

    if (rst) begin
        wr_ptr_reg <= '0;
        rd_ptr_reg <= '0;
        out_valid_reg <= 1'b0;
    end
end

endmodule

`resetall
