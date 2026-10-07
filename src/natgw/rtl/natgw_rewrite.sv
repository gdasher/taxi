// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: per-lane forward/punt decision and header rewrite

At each frame's first beat, one lookup result and one parser metadata record
are consumed (they arrive in frame order). The frame is then either forwarded
(MACs, VLAN ID, TTL, IP address, L4 port and checksums rewritten in bytes
0..63) to m_axis_fwd with tdest = egress lane, or punted unchanged to
m_axis_punt, optionally preceded by a 16-byte punt header.

Decision and checksum computation for the next frame run ahead in a two-entry
pipeline while the current frame streams, so back-to-back frames see no
bubbles (a punt with a header costs one extra beat for the header).

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_rewrite
    import natgw_pkg::*;
#(
    parameter LANE = 0,
    parameter USER_W = 49
)
(
    input  wire logic        clk,
    input  wire logic        rst,

    /*
     * Frames from the hold FIFO
     */
    taxi_axis_if.snk         s_axis_hold,

    /*
     * Lookup result and parser metadata (one each per frame, in order)
     */
    input  wire logic        s_res_valid,
    output wire logic        s_res_ready,
    input  wire result_t     s_res,

    input  wire logic        s_meta_valid,
    output wire logic        s_meta_ready,
    input  wire meta_t       s_meta,

    /*
     * Outputs
     */
    taxi_axis_if.src         m_axis_fwd,
    taxi_axis_if.src         m_axis_punt,

    /*
     * Configuration
     */
    input  wire logic        cfg_punt_hdr,
    input  wire logic [7:0]  cfg_egress_en,

    /*
     * Statistics
     */
    output wire logic        stat_valid,
    output wire logic [7:0]  stat_reason
);

localparam DATA_W = s_axis_hold.DATA_W;

localparam KEEP_W = s_axis_hold.KEEP_W;

// check configuration
if (DATA_W != 128 || KEEP_W != 16)
    $fatal(0, "Error: natgw_rewrite requires a 128-bit stream with tkeep (instance %m)");

if (s_axis_hold.USER_W != USER_W || m_axis_punt.USER_W != USER_W)
    $fatal(0, "Error: USER_W mismatch (instance %m)");

if (m_axis_fwd.DATA_W != DATA_W || m_axis_punt.DATA_W != DATA_W)
    $fatal(0, "Error: output DATA_W mismatch (instance %m)");

// ---------------------------------------------------------------------------
// stage 1: register result and metadata (joint pop)

logic    p1_valid_reg = 1'b0;
result_t p1_res_reg;
meta_t   p1_meta_reg;
// raw checksum sums, computed while loading p1 (folded in the p1 -> p2 step)
logic [19:0] p1_ip_sum_reg;
logic [19:0] p1_l4_sum_reg;

// checksum sums from the FIFO outputs
logic [31:0] s_old_ip;
logic [15:0] s_old_port;
logic [7:0]  s_proto;
logic [7:0]  s_nttl;

always_comb begin
    s_old_ip = s_res.xlate_dst ? s_meta.dip : s_meta.sip;
    s_old_port = s_res.xlate_dst ? s_meta.dport : s_meta.sport;
    s_proto = s_meta.tcp ? 8'd6 : 8'd17;
    s_nttl = s_res.dec_ttl ? s_meta.ttl - 8'd1 : s_meta.ttl;
end

// stage 2: decision and checksums
logic        p2_valid_reg = 1'b0;
logic        p2_fwd_reg;
logic [7:0]  p2_reason_reg;
logic        p2_hdr_reg;
logic [2:0]  p2_lane_reg;
logic [DATA_W-1:0] p2_hdr_beat_reg;
logic        p2_vlan_reg;
logic        p2_tcp_reg;
logic        p2_xlate_dst_reg;
logic [47:0] p2_dst_mac_reg;
logic [47:0] p2_src_mac_reg;
logic [15:0] p2_tci_reg;
logic [7:0]  p2_ttl_reg;
logic [15:0] p2_ip_csum_reg;
logic [31:0] p2_ip_reg;
logic [15:0] p2_port_reg;
logic [15:0] p2_l4_csum_reg;

logic load;
logic p2_ready_in;
logic p1_move;
logic p1_ready_in;

assign p2_ready_in = !p2_valid_reg || load;
assign p1_move = p1_valid_reg && p2_ready_in;
assign p1_ready_in = !p1_valid_reg || p1_move;

assign s_res_ready = p1_ready_in && s_res_valid && s_meta_valid;
assign s_meta_ready = p1_ready_in && s_res_valid && s_meta_valid;

always_ff @(posedge clk) begin
    if (p1_ready_in) begin
        p1_valid_reg <= s_res_valid && s_meta_valid;
        p1_res_reg <= s_res;
        p1_meta_reg <= s_meta;
        p1_ip_sum_reg <= csum_sum3(s_meta.ip_csum,
            s_old_ip[31:16], s_res.new_ip[31:16],
            s_old_ip[15:0], s_res.new_ip[15:0],
            {s_meta.ttl, s_proto}, {s_nttl, s_proto});
        p1_l4_sum_reg <= csum_sum3(s_meta.l4_csum,
            s_old_ip[31:16], s_res.new_ip[31:16],
            s_old_ip[15:0], s_res.new_ip[15:0],
            s_old_port, s_res.new_port);
    end

    if (rst) begin
        p1_valid_reg <= 1'b0;
    end
end

// decision (mirrors ShimModel.process after the lookup)
logic        d_fwd;
logic [7:0]  d_reason;
logic        d_hit;
logic [7:0]  d_proto;
logic [7:0]  d_nttl;
logic [31:0] d_old_ip;
logic [15:0] d_old_port;
logic [15:0] d_ip_csum;
logic [15:0] d_l4_csum;
logic [DATA_W-1:0] d_hdr_beat;
logic [7:0]  d_flags;
logic [31:0] d_hash;
logic [31:0] d_idx;

always_comb begin
    d_hit = p1_meta_reg.lookup && p1_res_reg.hit;
    d_fwd = 1'b0;

    if (p1_meta_reg.reason == RSN_MISS) begin
        if (d_hit) begin
            if (!p1_res_reg.nh.valid || !cfg_egress_en[p1_res_reg.nh.lane]) begin
                d_reason = RSN_NH;
            end else if (p1_res_reg.nh.vlan != p1_meta_reg.vlan) begin
                d_reason = RSN_VLAN;
            end else begin
                d_reason = RSN_FWD;
                d_fwd = 1'b1;
            end
        end else begin
            d_reason = RSN_MISS;
        end
    end else begin
        d_reason = p1_meta_reg.reason;
    end

    // checksums
    d_proto = p1_meta_reg.tcp ? 8'd6 : 8'd17;
    d_nttl = p1_res_reg.dec_ttl ? p1_meta_reg.ttl - 8'd1 : p1_meta_reg.ttl;
    d_old_ip = p1_res_reg.xlate_dst ? p1_meta_reg.dip : p1_meta_reg.sip;
    d_old_port = p1_res_reg.xlate_dst ? p1_meta_reg.dport : p1_meta_reg.sport;

    d_ip_csum = csum_fold(p1_ip_sum_reg);

    if (p1_meta_reg.tcp || p1_meta_reg.l4_csum != 0) begin
        d_l4_csum = csum_fold(p1_l4_sum_reg);
        if (!p1_meta_reg.tcp && d_l4_csum == 16'd0) begin
            d_l4_csum = 16'hffff;
        end
    end else begin
        d_l4_csum = 16'd0;
    end

    // punt header (big-endian fields, byte 0 in tdata[7:0])
    d_flags = {4'd0, d_hit, p1_meta_reg.vlan, 1'b0, p1_meta_reg.l3ok};
    d_hash = p1_meta_reg.lookup ? p1_res_reg.hash : 32'd0;
    d_idx = d_hit ? p1_res_reg.idx : 32'hffffffff;

    d_hdr_beat = '0;
    d_hdr_beat[0*8 +: 8] = PUNT_MAGIC[15:8];
    d_hdr_beat[1*8 +: 8] = PUNT_MAGIC[7:0];
    d_hdr_beat[2*8 +: 8] = PUNT_VER;
    d_hdr_beat[3*8 +: 8] = d_reason;
    d_hdr_beat[4*8 +: 8] = 8'(LANE);
    d_hdr_beat[5*8 +: 8] = d_flags;
    d_hdr_beat[6*8 +: 8] = p1_meta_reg.tci[15:8];
    d_hdr_beat[7*8 +: 8] = p1_meta_reg.tci[7:0];
    for (int k = 0; k < 4; k++) begin
        d_hdr_beat[(8+k)*8 +: 8] = d_hash[(3-k)*8 +: 8];
        d_hdr_beat[(12+k)*8 +: 8] = d_idx[(3-k)*8 +: 8];
    end
end

always_ff @(posedge clk) begin
    if (p2_ready_in) begin
        p2_valid_reg <= p1_valid_reg;

        p2_fwd_reg <= d_fwd;
        p2_reason_reg <= d_reason;
        p2_hdr_reg <= cfg_punt_hdr && !d_fwd && d_reason != RSN_BYPASS;
        p2_lane_reg <= p1_res_reg.nh.lane;
        p2_hdr_beat_reg <= d_hdr_beat;
        p2_vlan_reg <= p1_meta_reg.vlan;
        p2_tcp_reg <= p1_meta_reg.tcp;
        p2_xlate_dst_reg <= p1_res_reg.xlate_dst;
        p2_dst_mac_reg <= p1_res_reg.nh.dst_mac;
        p2_src_mac_reg <= p1_res_reg.nh.src_mac;
        p2_tci_reg <= {p1_meta_reg.tci[15:12], p1_res_reg.nh.vid};
        p2_ttl_reg <= d_nttl;
        p2_ip_csum_reg <= d_ip_csum;
        p2_ip_reg <= p1_res_reg.new_ip;
        p2_port_reg <= p1_res_reg.new_port;
        p2_l4_csum_reg <= d_l4_csum;
    end

    if (rst) begin
        p2_valid_reg <= 1'b0;
    end
end

// patch for bytes 0..63 (mirrors ShimModel.rewrite)
logic [511:0] q_patch;
logic [63:0]  q_mask;

always_comb begin
    int ip;
    int ipf;
    int portf;
    int l4cf;

    ip = p2_vlan_reg ? 18 : 14;
    ipf = ip + (p2_xlate_dst_reg ? 16 : 12);
    portf = ip + (p2_xlate_dst_reg ? 22 : 20);
    l4cf = ip + 20 + (p2_tcp_reg ? 16 : 6);

    q_patch = '0;
    q_mask = '0;

    for (int k = 0; k < 6; k++) begin
        q_patch[k*8 +: 8] = p2_dst_mac_reg[(5-k)*8 +: 8];
        q_patch[(6+k)*8 +: 8] = p2_src_mac_reg[(5-k)*8 +: 8];
    end
    q_mask[11:0] = '1;

    if (p2_vlan_reg) begin
        q_patch[14*8 +: 8] = p2_tci_reg[15:8];
        q_patch[15*8 +: 8] = p2_tci_reg[7:0];
        q_mask[15:14] = '1;
    end

    q_patch[(ip+8)*8 +: 8] = p2_ttl_reg;
    q_mask[ip+8] = 1'b1;

    q_patch[(ip+10)*8 +: 8] = p2_ip_csum_reg[15:8];
    q_patch[(ip+11)*8 +: 8] = p2_ip_csum_reg[7:0];
    q_mask[ip+10] = 1'b1;
    q_mask[ip+11] = 1'b1;

    for (int k = 0; k < 4; k++) begin
        q_patch[(ipf+k)*8 +: 8] = p2_ip_reg[(3-k)*8 +: 8];
        q_mask[ipf+k] = 1'b1;
    end

    q_patch[(portf+0)*8 +: 8] = p2_port_reg[15:8];
    q_patch[(portf+1)*8 +: 8] = p2_port_reg[7:0];
    q_mask[portf+0] = 1'b1;
    q_mask[portf+1] = 1'b1;

    q_patch[(l4cf+0)*8 +: 8] = p2_l4_csum_reg[15:8];
    q_patch[(l4cf+1)*8 +: 8] = p2_l4_csum_reg[7:0];
    q_mask[l4cf+0] = 1'b1;
    q_mask[l4cf+1] = 1'b1;

    if (!p2_fwd_reg) begin
        q_mask = '0;
    end
end

// ---------------------------------------------------------------------------
// streaming stage

logic        active_reg = 1'b0;
logic        fwd_reg = 1'b0;
logic        hdr_pending_reg = 1'b0;
logic [2:0]  lane_reg = '0;
logic [DATA_W-1:0] hdr_beat_reg = '0;
logic [511:0] patch_reg = '0;
logic [63:0] mask_reg = '0;
logic [2:0]  beat_reg = '0;

logic        stat_valid_reg = 1'b0;
logic [7:0]  stat_reason_reg = '0;

taxi_axis_if #(
    .DATA_W(DATA_W),
    .KEEP_EN(1),
    .KEEP_W(KEEP_W),
    .LAST_EN(1),
    .DEST_EN(1),
    .DEST_W(m_axis_fwd.DEST_W),
    .USER_EN(1),
    .USER_W(m_axis_fwd.USER_W)
) fwd_int();

taxi_axis_if #(
    .DATA_W(DATA_W),
    .KEEP_EN(1),
    .KEEP_W(KEEP_W),
    .LAST_EN(1),
    .USER_EN(1),
    .USER_W(USER_W)
) punt_int();

logic [127:0] beat_patch;
logic [15:0]  beat_mask;
logic [127:0] beat_data;
logic         hold_xfer;
logic         last_xfer;

always_comb begin
    beat_patch = patch_reg[beat_reg[1:0]*128 +: 128];
    beat_mask = beat_reg[2] ? 16'd0 : mask_reg[beat_reg[1:0]*16 +: 16];
    beat_mask = beat_mask & s_axis_hold.tkeep;
    for (int k = 0; k < 16; k++) begin
        beat_data[k*8 +: 8] = beat_mask[k] ? beat_patch[k*8 +: 8] : s_axis_hold.tdata[k*8 +: 8];
    end
end

assign fwd_int.tdata = beat_data;
assign fwd_int.tkeep = s_axis_hold.tkeep;
assign fwd_int.tstrb = s_axis_hold.tkeep;
assign fwd_int.tvalid = active_reg && fwd_reg && s_axis_hold.tvalid;
assign fwd_int.tlast = s_axis_hold.tlast;
assign fwd_int.tid = '0;
assign fwd_int.tdest = lane_reg;
assign fwd_int.tuser = '0;

assign punt_int.tdata = hdr_pending_reg ? hdr_beat_reg : s_axis_hold.tdata;
assign punt_int.tkeep = hdr_pending_reg ? '1 : s_axis_hold.tkeep;
assign punt_int.tstrb = punt_int.tkeep;
assign punt_int.tvalid = active_reg && !fwd_reg && s_axis_hold.tvalid;
assign punt_int.tlast = hdr_pending_reg ? 1'b0 : s_axis_hold.tlast;
assign punt_int.tid = '0;
assign punt_int.tdest = '0;
assign punt_int.tuser = s_axis_hold.tuser;

assign s_axis_hold.tready = active_reg && !hdr_pending_reg && (fwd_reg ? fwd_int.tready : punt_int.tready);

assign hold_xfer = s_axis_hold.tvalid && s_axis_hold.tready;
assign last_xfer = hold_xfer && s_axis_hold.tlast;

assign load = p2_valid_reg && (!active_reg || last_xfer);

always_ff @(posedge clk) begin
    stat_valid_reg <= 1'b0;

    if (hold_xfer && !beat_reg[2]) begin
        beat_reg <= beat_reg + 1;
    end

    if (hdr_pending_reg && punt_int.tvalid && punt_int.tready) begin
        hdr_pending_reg <= 1'b0;
    end

    if (last_xfer) begin
        active_reg <= 1'b0;
    end

    if (load) begin
        active_reg <= 1'b1;
        fwd_reg <= p2_fwd_reg;
        hdr_pending_reg <= p2_hdr_reg;
        lane_reg <= p2_lane_reg;
        hdr_beat_reg <= p2_hdr_beat_reg;
        patch_reg <= q_patch;
        mask_reg <= q_mask;
        beat_reg <= '0;

        stat_valid_reg <= 1'b1;
        stat_reason_reg <= p2_reason_reg;
    end

    if (rst) begin
        active_reg <= 1'b0;
        hdr_pending_reg <= 1'b0;
        beat_reg <= '0;
        stat_valid_reg <= 1'b0;
    end
end

assign stat_valid = stat_valid_reg;
assign stat_reason = stat_reason_reg;

// output registers
taxi_axis_register #(
    .REG_TYPE(2)
)
fwd_reg_inst (
    .clk(clk),
    .rst(rst),
    .s_axis(fwd_int),
    .m_axis(m_axis_fwd)
);

taxi_axis_register #(
    .REG_TYPE(2)
)
punt_reg_inst (
    .clk(clk),
    .rst(rst),
    .s_axis(punt_int),
    .m_axis(m_axis_punt)
);

endmodule

`resetall
