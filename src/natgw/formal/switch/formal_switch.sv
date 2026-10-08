// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_switch (LANES inputs and outputs)

Inputs present arbitrary frames (tdest constant within a frame, valid/ready
rules respected); outputs stall arbitrarily. Every beat carries its source
lane and a per-source sequence number in tdata so delivery can be checked.

  P6.1  each output beat came from an input whose frame targets that output
  P6.2  frames are not interleaved: once an output starts a frame from input
        i, every beat until tlast comes from input i
  P6.3  per (input, output) pair, beats arrive in order with none lost or
        duplicated (per-pair sequence numbers are consecutive at the output);
        frames from one input to different outputs are independent
  P6.4  outputs obey valid/ready (hold steady while stalled)

*/

`default_nettype none

module formal_switch #(
    parameter LANES = 4
)
(
    input wire logic       clk,
    input wire logic       in_go[LANES],
    input wire logic       in_last[LANES],
    input wire logic [1:0] in_dest[LANES],
    input wire logic       out_ready[LANES]
);

localparam CL = $clog2(LANES);

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

taxi_axis_if #(.DATA_W(16), .KEEP_W(1), .DEST_EN(1), .DEST_W(CL), .USER_EN(1), .USER_W(1)) s_axis[LANES]();
taxi_axis_if #(.DATA_W(16), .KEEP_W(1), .DEST_EN(1), .DEST_W(1), .USER_EN(1), .USER_W(1)) m_axis[LANES]();

// sources: tdata = {lane[3:0], dest[3:0], per-(lane, dest) sequence[7:0]}
logic [7:0]    seq[LANES][LANES];
logic          sv[LANES];
logic          sl[LANES];
logic [CL-1:0] sd[LANES];
logic          in_frame[LANES];

for (genvar i = 0; i < LANES; i = i + 1) begin : src
    assign s_axis[i].tdata = {4'(i), 4'(sd[i]), seq[i][sd[i]]};
    assign s_axis[i].tkeep = 1'b1;
    assign s_axis[i].tstrb = 1'b1;
    assign s_axis[i].tvalid = sv[i];
    assign s_axis[i].tlast = sl[i];
    assign s_axis[i].tid = '0;
    assign s_axis[i].tdest = sd[i];
    assign s_axis[i].tuser = 1'b0;

    always_ff @(posedge clk) begin
        if (sv[i] && s_axis[i].tready) begin
            sv[i] <= 1'b0;
            seq[i][sd[i]] <= seq[i][sd[i]] + 1;
            in_frame[i] <= !sl[i];
        end
        if ((!sv[i] || s_axis[i].tready) && in_go[i]) begin
            sv[i] <= 1'b1;
            sl[i] <= in_last[i];
            // tdest is chosen at the start of a frame and held
            if (!(in_frame[i] || (sv[i] && s_axis[i].tready && !sl[i]))) sd[i] <= CL'(in_dest[i]);
        end
        if (rst) begin
            sv[i] <= 1'b0;
            for (int d = 0; d < LANES; d++) seq[i][d] <= '0;
            in_frame[i] <= 1'b0;
        end
    end
end

natgw_switch #(
    .LANES(LANES)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_axis(s_axis),
    .m_axis(m_axis)
);

// outputs
logic          busy[LANES];
logic [7:0]    cur_src[LANES];
logic          p_stall[LANES];
logic [15:0]   p_data[LANES];
logic          p_last[LANES];

for (genvar j = 0; j < LANES; j = j + 1) begin : out
    assign m_axis[j].tready = out_ready[j];
    wire [7:0] src = 8'(m_axis[j].tdata[15:12]);
    wire [3:0] dst = m_axis[j].tdata[11:8];
    wire [7:0] sq = m_axis[j].tdata[7:0];

    always_ff @(posedge clk) begin
        p_stall[j] <= !rst && m_axis[j].tvalid && !m_axis[j].tready;
        p_data[j] <= m_axis[j].tdata;
        p_last[j] <= m_axis[j].tlast;
        if (!rst && p_stall[j]) begin
            assert (m_axis[j].tvalid);                       // P6.4
            assert (m_axis[j].tdata == p_data[j]);
            assert (m_axis[j].tlast == p_last[j]);
        end
        if (!rst && m_axis[j].tvalid && m_axis[j].tready) begin
            assert (src < 8'(LANES));
            assert (dst == 4'(j));                             // P6.1
            if (busy[j]) assert (src == cur_src[j]);          // P6.2
            busy[j] <= !m_axis[j].tlast;
            cur_src[j] <= src;
        end
        if (rst) begin
            busy[j] <= 1'b0;
        end
    end
end

// P6.3 per (source, output): consecutive sequence numbers
for (genvar j = 0; j < LANES; j = j + 1) begin : pair
    logic [7:0] es[LANES];               // next expected sequence per source
    wire       fire = m_axis[j].tvalid && m_axis[j].tready;
    wire [3:0] s4 = m_axis[j].tdata[15:12];
    always_ff @(posedge clk) begin
        if (!rst && fire) begin
            assert (m_axis[j].tdata[7:0] == es[s4[CL-1:0]]);
            es[s4[CL-1:0]] <= m_axis[j].tdata[7:0] + 1;
        end
        if (rst) for (int i = 0; i < LANES; i++) es[i] <= '0;
    end
end

always_ff @(posedge clk) begin
    if (!rst) cover (pair[0].es[1] >= 8'd2 && pair[1].es[0] >= 8'd2);
end

endmodule
