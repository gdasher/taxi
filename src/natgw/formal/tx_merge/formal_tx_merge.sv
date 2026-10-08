// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_tx_merge

The core and the shim present arbitrary frames (each beat tagged with its
source and a sequence number in tdata); the MAC stalls arbitrarily and
returns arbitrary completions.

  P8.1  core frames leave with tid 0 and shim frames with tid 1
  P8.2  frames are not interleaved at the MAC
  P8.3  each source's beats reach the MAC exactly once, in order
  P8.4  only tid-0 completions reach the core, every one of them does, and
        tid-non-0 completions are always consumed (never block the MAC)

*/

`default_nettype none

module formal_tx_merge (
    input wire logic       clk,
    input wire logic       go[2],
    input wire logic       last[2],
    input wire logic       mac_ready,
    input wire logic       cpl_valid,
    input wire logic [7:0] cpl_tid,
    input wire logic [7:0] cpl_data,
    input wire logic       core_cpl_ready
);

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

taxi_axis_if #(.DATA_W(16), .KEEP_W(1), .USER_EN(1), .USER_W(1)) core_axis(), shim_axis();
taxi_axis_if #(.DATA_W(16), .KEEP_W(1), .ID_EN(1), .ID_W(8), .USER_EN(1), .USER_W(1)) mac_axis();
taxi_axis_if #(.DATA_W(8), .KEEP_W(1), .ID_EN(1), .ID_W(8)) mac_cpl(), core_cpl();

logic [6:0] seq[2];
logic       sv[2];
logic       sl[2];

for (genvar i = 0; i < 2; i = i + 1) begin : src
    always_ff @(posedge clk) begin
        if (sv[i] && (i == 0 ? core_axis.tready : shim_axis.tready)) begin
            sv[i] <= 1'b0;
            seq[i] <= seq[i] + 1;
        end
        if ((!sv[i] || (i == 0 ? core_axis.tready : shim_axis.tready)) && go[i]) begin
            sv[i] <= 1'b1;
            sl[i] <= last[i];
        end
        if (rst) begin
            sv[i] <= 1'b0;
            seq[i] <= '0;
        end
    end
end

assign core_axis.tdata = {1'b0, seq[0], 8'd0};
assign core_axis.tkeep = 1'b1;
assign core_axis.tstrb = 1'b1;
assign core_axis.tvalid = sv[0];
assign core_axis.tlast = sl[0];
assign core_axis.tid = '0;
assign core_axis.tdest = '0;
assign core_axis.tuser = 1'b0;

assign shim_axis.tdata = {1'b1, seq[1], 8'd0};
assign shim_axis.tkeep = 1'b1;
assign shim_axis.tstrb = 1'b1;
assign shim_axis.tvalid = sv[1];
assign shim_axis.tlast = sl[1];
assign shim_axis.tid = '0;
assign shim_axis.tdest = '0;
assign shim_axis.tuser = 1'b0;

assign mac_axis.tready = mac_ready;

assign mac_cpl.tdata = cpl_data;
assign mac_cpl.tkeep = 1'b1;
assign mac_cpl.tstrb = 1'b1;
assign mac_cpl.tvalid = cpl_valid;
assign mac_cpl.tlast = 1'b1;
assign mac_cpl.tid = cpl_tid;
assign mac_cpl.tdest = '0;
assign mac_cpl.tuser = '0;
assign core_cpl.tready = core_cpl_ready;

natgw_tx_merge dut (
    .clk(clk),
    .rst(rst),
    .s_axis_core(core_axis),
    .s_axis_shim(shim_axis),
    .m_axis_mac(mac_axis),
    .s_axis_mac_cpl(mac_cpl),
    .m_axis_core_cpl(core_cpl)
);

logic       busy = 1'b0;
logic       cur;
logic [6:0] out_seq[2];

always_ff @(posedge clk) begin
    if (!rst && mac_axis.tvalid && mac_axis.tready) begin
        automatic logic s = mac_axis.tdata[15];
        assert (mac_axis.tid == 8'(s));                       // P8.1
        if (busy) assert (s == cur);                          // P8.2
        assert (mac_axis.tdata[14:8] == out_seq[s]);          // P8.3
        out_seq[s] <= out_seq[s] + 1;
        busy <= !mac_axis.tlast;
        cur <= s;
    end
    if (rst) begin
        busy <= 1'b0;
        out_seq[0] <= '0;
        out_seq[1] <= '0;
    end
end

// P8.4 completion filter
always_comb begin
    if (!rst) begin
        assert (core_cpl.tvalid == (cpl_valid && cpl_tid == 8'd0));
        if (core_cpl.tvalid) assert (core_cpl.tdata == cpl_data && core_cpl.tid == 8'd0);
        if (cpl_valid && cpl_tid != 8'd0) assert (mac_cpl.tready);
        if (cpl_valid && cpl_tid == 8'd0) assert (mac_cpl.tready == core_cpl_ready);
    end
end

always_ff @(posedge clk) if (!rst) cover (out_seq[0] >= 7'd2 && out_seq[1] >= 7'd2);

endmodule
