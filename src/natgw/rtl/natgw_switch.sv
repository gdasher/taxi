// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: lane switch

Routes whole frames from LANES inputs to LANES outputs by tdest (the egress
lane), with round-robin arbitration per output. A grant is held until the
frame's last beat. Each input and output has a skid register.

(Used instead of taxi_axis_switch, whose untyped unpacked-array parameters
Vivado 2026.1 does not accept.)

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_switch #(
    parameter LANES = 8
)
(
    input  wire logic  clk,
    input  wire logic  rst,

    taxi_axis_if.snk   s_axis[LANES],
    taxi_axis_if.src   m_axis[LANES]
);

localparam CL_LANES = $clog2(LANES);
localparam DATA_W = s_axis[0].DATA_W;
localparam KEEP_W = s_axis[0].KEEP_W;
localparam USER_W = s_axis[0].USER_W;

// flatten the inputs
wire [DATA_W-1:0]    s_tdata[LANES];
wire [KEEP_W-1:0]    s_tkeep[LANES];
wire                 s_tvalid[LANES];
logic                s_tready[LANES];
wire                 s_tlast[LANES];
wire [CL_LANES-1:0]  s_tdest[LANES];
wire [USER_W-1:0]    s_tuser[LANES];

for (genvar i = 0; i < LANES; i = i + 1) begin : in

    // input skid register: keeps the arbitration and ready paths local
    taxi_axis_if #(
        .DATA_W(DATA_W),
        .KEEP_W(KEEP_W),
        .DEST_EN(1),
        .DEST_W(s_axis[0].DEST_W),
        .USER_EN(1),
        .USER_W(USER_W)
    ) in_axis();

    taxi_axis_register #(
        .REG_TYPE(2)
    )
    in_reg_inst (
        .clk(clk),
        .rst(rst),
        .s_axis(s_axis[i]),
        .m_axis(in_axis)
    );

    assign s_tdata[i] = in_axis.tdata;
    assign s_tkeep[i] = in_axis.tkeep;
    assign s_tvalid[i] = in_axis.tvalid;
    assign in_axis.tready = s_tready[i];
    assign s_tlast[i] = in_axis.tlast;
    assign s_tdest[i] = CL_LANES'(in_axis.tdest);
    assign s_tuser[i] = in_axis.tuser;

end

// per-output arbitration state
wire                 busy[LANES];
wire [CL_LANES-1:0]  grant[LANES];
wire                 out_tready[LANES];

always_comb begin
    for (int i = 0; i < LANES; i++) begin
        s_tready[i] = 1'b0;
    end
    for (int j = 0; j < LANES; j++) begin
        if (busy[j]) begin
            s_tready[grant[j]] = out_tready[j];
        end
    end
end

for (genvar j = 0; j < LANES; j = j + 1) begin : out

    logic                busy_reg = 1'b0;
    logic [CL_LANES-1:0] grant_reg = '0;
    logic [CL_LANES-1:0] rr_reg = '0;

    assign busy[j] = busy_reg;
    assign grant[j] = grant_reg;

    logic [LANES-1:0] req;

    always_comb begin
        for (int i = 0; i < LANES; i++) begin
            req[i] = s_tvalid[i] && s_tdest[i] == CL_LANES'(j);
        end
    end

    // round robin: first requester at or after rr_reg
    logic                pick_valid;
    logic [CL_LANES-1:0] pick;

    always_comb begin
        pick_valid = 1'b0;
        pick = '0;
        for (int k = LANES-1; k >= 0; k--) begin
            if (req[CL_LANES'(rr_reg + CL_LANES'(k))]) begin
                pick_valid = 1'b1;
                pick = CL_LANES'(rr_reg + CL_LANES'(k));
            end
        end
    end

    taxi_axis_if #(
        .DATA_W(DATA_W),
        .KEEP_W(KEEP_W),
        .DEST_EN(m_axis[0].DEST_EN),
        .DEST_W(m_axis[0].DEST_W),
        .USER_EN(1),
        .USER_W(USER_W)
    ) mux_axis();

    wire [CL_LANES-1:0] g = grant_reg;

    assign mux_axis.tdata = s_tdata[g];
    assign mux_axis.tkeep = s_tkeep[g];
    assign mux_axis.tstrb = s_tkeep[g];
    assign mux_axis.tvalid = busy_reg && s_tvalid[g];
    assign mux_axis.tlast = s_tlast[g];
    assign mux_axis.tid = '0;
    assign mux_axis.tdest = '0;
    assign mux_axis.tuser = s_tuser[g];
    assign out_tready[j] = mux_axis.tready;

    always_ff @(posedge clk) begin
        if (busy_reg) begin
            if (mux_axis.tvalid && mux_axis.tready && mux_axis.tlast) begin
                busy_reg <= 1'b0;
                rr_reg <= grant_reg + 1;
            end
        end else if (pick_valid) begin
            busy_reg <= 1'b1;
            grant_reg <= pick;
        end

        if (rst) begin
            busy_reg <= 1'b0;
            grant_reg <= '0;
            rr_reg <= '0;
        end
    end

    taxi_axis_register #(
        .REG_TYPE(2)
    )
    out_reg_inst (
        .clk(clk),
        .rst(rst),
        .s_axis(mux_axis),
        .m_axis(m_axis[j])
    );

end

endmodule

`resetall
