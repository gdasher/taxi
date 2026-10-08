// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_ram (banked) is equivalent to a plain single-array memory with
the same contract: inputs registered, a write visible to reads presented in
the following cycle or later, read data PIPE cycles after an enabled read,
and a read in the same cycle as a write to that address returns the old data.

  P9.1  every enabled port-A read of a written address returns the reference
        value, PIPE cycles later
  P9.2  the same for port-B reads (port B also takes the writes)

Configurations cover one bank, one mux level and two mux levels, each at the
minimum PIPE, with a bank output register, and with an extra output stage.

*/

`default_nettype none

module formal_ram #(
    parameter ADDR_W = 5,
    parameter BANK_AW = 2,
    parameter PIPE = 3,
    parameter DATA_W = 8
)
(
    input wire logic              clk,
    input wire logic              a_en,
    input wire logic [ADDR_W-1:0] a_addr,
    input wire logic              b_en,
    input wire logic              b_we,
    input wire logic [ADDR_W-1:0] b_addr,
    input wire logic [DATA_W-1:0] b_din
);

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;

wire [DATA_W-1:0] a_dout, b_dout;

natgw_ram #(
    .DATA_W(DATA_W),
    .ADDR_W(ADDR_W),
    .PIPE(PIPE),
    .BANK_AW(BANK_AW),
    .RAM_STYLE("ultra")
)
dut (
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

// reference: one array, same timing contract
logic [DATA_W-1:0] ref_mem[2**ADDR_W];
logic              written[2**ADDR_W];

logic              ra_en = 1'b0, rb_en = 1'b0, rb_we = 1'b0;
logic [ADDR_W-1:0] ra_addr, rb_addr;
logic [DATA_W-1:0] rb_din;

// read pipeline of the reference: data, enable, "address was written" flag
logic [DATA_W-1:0] qa[PIPE], qb[PIPE];
logic              va[PIPE], vb[PIPE];

always_ff @(posedge clk) begin
    ra_en <= a_en;
    ra_addr <= a_addr;
    rb_en <= b_en;
    rb_we <= b_we;
    rb_addr <= b_addr;
    rb_din <= b_din;

    // stage 1 (one cycle after the registered inputs): access, old data on a collision
    qa[1] <= ref_mem[ra_addr];
    va[1] <= ra_en && written[ra_addr];
    qb[1] <= ref_mem[rb_addr];
    vb[1] <= rb_en && !rb_we && written[rb_addr];
    if (rb_en && rb_we) begin
        ref_mem[rb_addr] <= rb_din;
        written[rb_addr] <= 1'b1;
    end
    for (int i = 2; i < PIPE; i++) begin
        qa[i] <= qa[i-1];
        va[i] <= va[i-1];
        qb[i] <= qb[i-1];
        vb[i] <= vb[i-1];
    end

    if (init) begin
        ra_en <= 1'b0;
        rb_en <= 1'b0;
        for (int i = 0; i < 2**ADDR_W; i++) written[i] <= 1'b0;
        for (int i = 1; i < PIPE; i++) begin
            va[i] <= 1'b0;
            vb[i] <= 1'b0;
        end
    end
end

always_comb begin
    if (!init) begin
        if (va[PIPE-1]) assert (a_dout == qa[PIPE-1]);    // P9.1
        if (vb[PIPE-1]) assert (b_dout == qb[PIPE-1]);    // P9.2
    end
end

always_ff @(posedge clk) begin
    if (!init) cover (va[PIPE-1] && vb[PIPE-1]);
end

endmodule
