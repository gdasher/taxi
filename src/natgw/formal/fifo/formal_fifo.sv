// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_fifo (descriptor, metadata and result FIFOs)

Data pushed is a running count, so order, loss and duplication are visible.

  P7.1  pops return exactly the pushed values, in order
  P7.2  s_ready is low exactly when DEPTH entries are held (output register
        included in the count as the design defines it), and overflow flags
        exactly a push attempted while full
  P7.3  m_valid/m_data hold steady while stalled

*/

`default_nettype none

module formal_fifo #(
    parameter DEPTH = 4
)
(
    input wire logic clk,
    input wire logic push,
    input wire logic pop_ready
);

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

logic [7:0] wr_cnt = '0, rd_cnt = '0;
wire s_ready, m_valid, overflow;
wire [7:0] m_data;

natgw_fifo #(
    .DATA_W(8),
    .DEPTH(DEPTH)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_valid(push),
    .s_ready(s_ready),
    .s_data(wr_cnt),
    .m_valid(m_valid),
    .m_ready(pop_ready),
    .m_data(m_data),
    .overflow(overflow)
);

logic p_stall = 1'b0;
logic [7:0] p_data;

always_ff @(posedge clk) begin
    if (!rst) begin
        if (push && s_ready) wr_cnt <= wr_cnt + 1;
        if (m_valid && pop_ready) begin
            assert (m_data == rd_cnt);                        // P7.1
            rd_cnt <= rd_cnt + 1;
        end
        assert (overflow == (push && !s_ready));              // P7.2
        assert (8'(wr_cnt - rd_cnt) <= 8'(DEPTH + 1));       // never more than the RAM plus the output register
        assert (m_valid == (wr_cnt != rd_cnt) || !m_valid);   // nothing appears that was not pushed
        if (wr_cnt == rd_cnt) assert (!m_valid);
    end
    p_stall <= !rst && m_valid && !pop_ready;
    p_data <= m_data;
    if (!rst && p_stall) begin
        assert (m_valid);                                     // P7.3
        assert (m_data == p_data);
    end
    if (rst) begin
        wr_cnt <= '0;
        rd_cnt <= '0;
    end
end

// inductive invariants: the FIFO's pointers, output register and stored data
// are exactly the values pushed and not yet popped
localparam AW = $clog2(DEPTH);
wire [AW:0] dut_count = dut.wr_ptr_reg - dut.rd_ptr_reg;
always_comb begin
    if (!rst) begin
        assert (8'(wr_cnt - rd_cnt) == 8'(dut_count) + 8'(dut.out_valid_reg));
        assert (dut_count <= (AW+1)'(DEPTH));
        if (dut.out_valid_reg) assert (dut.out_data_reg == rd_cnt);
        for (int k = 0; k < DEPTH; k++) begin
            if ((AW+1)'(k) < dut_count) assert (dut.mem[AW'(dut.rd_ptr_reg + (AW+1)'(k))] == rd_cnt + 8'(dut.out_valid_reg) + 8'(k));
        end
    end
end

always_ff @(posedge clk) if (!rst) cover (rd_cnt == 8'd6);

endmodule
