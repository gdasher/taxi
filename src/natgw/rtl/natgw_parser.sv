// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: per-lane parser and exception classifier

Passes every beat through to the hold FIFO unchanged, captures the first 64
bytes of each frame and, after the last beat, emits one descriptor (lookup
key, flags, length and per-frame metadata). Classification matches
natgw_model.parse() exactly. Three pipeline stages after EOP; the whole
parser stalls while a descriptor is held.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_parser
    import natgw_pkg::*;
#(
    parameter LANE = 0,
    /* verilator lint_off UNUSEDPARAM */
    parameter USER_W = 49  // tuser width (passed through; documents the interface)
    /* verilator lint_on UNUSEDPARAM */
)
(
    input  wire logic         clk,
    input  wire logic         rst,

    taxi_axis_if.snk          s_axis,
    taxi_axis_if.src          m_axis_hold,

    output wire logic         m_desc_valid,
    input  wire logic         m_desc_ready,
    output wire key_t         m_desc_key,
    output wire logic         m_desc_lookup,
    output wire logic         m_desc_fin,
    output wire logic         m_desc_rst,
    output wire logic [15:0]  m_desc_len,
    output wire meta_t        m_desc_meta,

    input  wire logic         cfg_bypass
);

localparam DATA_W = s_axis.DATA_W;
localparam KEEP_W = s_axis.KEEP_W;

if (DATA_W != 128 || KEEP_W != 16) begin : check_width
    $fatal(0, "Error: natgw_parser requires 128-bit data with 16-bit tkeep (instance %m)");
end

// pipeline enable: everything advances unless the output descriptor is held
wire pipe_en = !m_desc_valid || m_desc_ready;

// passthrough to the hold FIFO, through a skid register: pipe_en can fall
// while a beat is being offered, and the register (which only captures on a
// handshake) keeps m_axis_hold within the valid/ready rules
taxi_axis_if #(
    .DATA_W(DATA_W),
    .KEEP_W(KEEP_W),
    .ID_EN(s_axis.ID_EN),
    .ID_W(s_axis.ID_W),
    .DEST_EN(s_axis.DEST_EN),
    .DEST_W(s_axis.DEST_W),
    .USER_EN(s_axis.USER_EN),
    .USER_W(s_axis.USER_W)
) hold_int();

assign hold_int.tdata  = s_axis.tdata;
assign hold_int.tkeep  = s_axis.tkeep;
assign hold_int.tstrb  = s_axis.tstrb;
assign hold_int.tid    = s_axis.tid;
assign hold_int.tdest  = s_axis.tdest;
assign hold_int.tuser  = s_axis.tuser;
assign hold_int.tlast  = s_axis.tlast;
assign hold_int.tvalid = s_axis.tvalid && pipe_en;

assign s_axis.tready = hold_int.tready && pipe_en;

taxi_axis_register #(
    .REG_TYPE(2)
)
hold_reg_inst (
    .clk(clk),
    .rst(rst),
    .s_axis(hold_int),
    .m_axis(m_axis_hold)
);

wire beat = s_axis.tvalid && s_axis.tready;

// header capture
logic [511:0] hdr_reg = '0;
logic [11:0] beat_cnt_reg = '0;

logic [127:0] beat_data;
logic [511:0] hdr_next;
logic [4:0] keep_cnt;

always_comb begin
    for (int i = 0; i < 16; i++) begin
        beat_data[i*8 +: 8] = s_axis.tkeep[i] ? s_axis.tdata[i*8 +: 8] : 8'd0;
    end

    keep_cnt = '0;
    for (int i = 0; i < 16; i++) begin
        keep_cnt = keep_cnt + 5'(s_axis.tkeep[i]);
    end

    hdr_next = beat_cnt_reg == 0 ? '0 : hdr_reg;
    if (beat_cnt_reg < 4) begin
        hdr_next[beat_cnt_reg[1:0]*128 +: 128] = beat_data;
    end
end

// stage 1: captured header
logic [511:0] s1_hdr_reg = '0;
logic [15:0] s1_len_reg = '0;
logic s1_bypass_reg = 1'b0;
logic s1_valid_reg = 1'b0;

always_ff @(posedge clk) begin
    if (beat) begin
        hdr_reg <= hdr_next;
        if (s_axis.tlast) begin
            beat_cnt_reg <= '0;
        end else if (beat_cnt_reg != '1) begin
            beat_cnt_reg <= beat_cnt_reg + 1;
        end
    end

    if (pipe_en) begin
        s1_valid_reg <= beat && s_axis.tlast;
        if (beat && s_axis.tlast) begin
            s1_hdr_reg <= hdr_next;
            s1_len_reg <= {beat_cnt_reg, 4'd0} + 16'(keep_cnt);
            s1_bypass_reg <= cfg_bypass;
        end
    end

    if (rst) begin
        beat_cnt_reg <= '0;
        s1_valid_reg <= 1'b0;
    end
end

// stage 1 -> 2: field extraction and raw header sum
function automatic logic [7:0] hb(input logic [511:0] h, input int i);
    return h[i*8 +: 8];
endfunction

function automatic logic [15:0] hw(input logic [511:0] h, input int i);
    return {h[i*8 +: 8], h[(i+1)*8 +: 8]};
endfunction

function automatic logic [31:0] hd(input logic [511:0] h, input int i);
    return {hw(h, i), hw(h, i+2)};
endfunction

logic        a_vlan;
logic [15:0] a_tci;
logic [15:0] a_et;
logic [7:0]  a_ver_ihl;
logic [15:0] a_totlen;
logic [15:0] a_frag;
logic [7:0]  a_ttl;
logic [7:0]  a_proto;
logic [15:0] a_ip_csum;
logic [31:0] a_sip;
logic [31:0] a_dip;
logic [15:0] a_sport;
logic [15:0] a_dport;
logic [7:0]  a_tcp_flags;
logic [15:0] a_l4_csum;
logic [19:0] a_sum;
logic [16:0] a_ip_end;   // ip offset + 20
logic [16:0] a_ip_tot;   // ip offset + totlen

always_comb begin
    logic [511:0] h;
    h = s1_hdr_reg;

    a_vlan = hw(h, 12) == 16'h8100;

    if (a_vlan) begin
        a_tci = hw(h, 14);
        a_et = hw(h, 16);
        a_ver_ihl = hb(h, 18);
        a_totlen = hw(h, 20);
        a_frag = hw(h, 24);
        a_ttl = hb(h, 26);
        a_proto = hb(h, 27);
        a_ip_csum = hw(h, 28);
        a_sip = hd(h, 30);
        a_dip = hd(h, 34);
        a_sport = hw(h, 38);
        a_dport = hw(h, 40);
        a_tcp_flags = hb(h, 51);
        a_l4_csum = hb(h, 27) == 8'd6 ? hw(h, 54) : hw(h, 44);
        a_sum = '0;
        for (int i = 0; i < 10; i++) begin
            a_sum = a_sum + 20'(hw(h, 18+2*i));
        end
        a_ip_end = 17'd38;
        a_ip_tot = 17'd18 + 17'(hw(h, 20));
    end else begin
        a_tci = '0;
        a_et = hw(h, 12);
        a_ver_ihl = hb(h, 14);
        a_totlen = hw(h, 16);
        a_frag = hw(h, 20);
        a_ttl = hb(h, 22);
        a_proto = hb(h, 23);
        a_ip_csum = hw(h, 24);
        a_sip = hd(h, 26);
        a_dip = hd(h, 30);
        a_sport = hw(h, 34);
        a_dport = hw(h, 36);
        a_tcp_flags = hb(h, 47);
        a_l4_csum = hb(h, 23) == 8'd6 ? hw(h, 50) : hw(h, 40);
        a_sum = '0;
        for (int i = 0; i < 10; i++) begin
            a_sum = a_sum + 20'(hw(h, 14+2*i));
        end
        a_ip_end = 17'd34;
        a_ip_tot = 17'd14 + 17'(hw(h, 16));
    end
end

// stage 2: extracted fields
logic        s2_valid_reg = 1'b0;
logic        s2_bypass_reg = 1'b0;
logic [15:0] s2_len_reg = '0;
logic        s2_mcast_reg = 1'b0;
logic        s2_vlan_reg = 1'b0;
logic [15:0] s2_tci_reg = '0;
logic        s2_is_ipv4_reg = 1'b0;
logic        s2_hdr_ok_reg = 1'b0;
logic [19:0] s2_sum_reg = '0;
logic [15:0] s2_totlen_reg = '0;
logic [15:0] s2_frag_reg = '0;
logic [7:0]  s2_ttl_reg = '0;
logic [7:0]  s2_proto_reg = '0;
logic [15:0] s2_ip_csum_reg = '0;
logic [31:0] s2_sip_reg = '0;
logic [31:0] s2_dip_reg = '0;
logic [15:0] s2_sport_reg = '0;
logic [15:0] s2_dport_reg = '0;
logic [7:0]  s2_tcp_flags_reg = '0;
logic [15:0] s2_l4_csum_reg = '0;

always_ff @(posedge clk) begin
    if (pipe_en) begin
        s2_valid_reg <= s1_valid_reg;
        s2_bypass_reg <= s1_bypass_reg;
        s2_len_reg <= s1_len_reg;
        s2_mcast_reg <= s1_hdr_reg[0];
        s2_vlan_reg <= a_vlan;
        s2_tci_reg <= a_tci;
        s2_is_ipv4_reg <= a_et == 16'h0800;
        s2_hdr_ok_reg <= a_ver_ihl == 8'h45 && 17'(s1_len_reg) >= a_ip_end
            && a_totlen >= 16'd20 && a_ip_tot <= 17'(s1_len_reg);
        s2_sum_reg <= a_sum;
        s2_totlen_reg <= a_totlen;
        s2_frag_reg <= a_frag;
        s2_ttl_reg <= a_ttl;
        s2_proto_reg <= a_proto;
        s2_ip_csum_reg <= a_ip_csum;
        s2_sip_reg <= a_sip;
        s2_dip_reg <= a_dip;
        s2_sport_reg <= a_sport;
        s2_dport_reg <= a_dport;
        s2_tcp_flags_reg <= a_tcp_flags;
        s2_l4_csum_reg <= a_l4_csum;
    end

    if (rst) begin
        s2_valid_reg <= 1'b0;
    end
end

// stage 2 -> 3: classification
logic [16:0] b_fold;
logic        b_csum_ok;
logic        b_tcp;
logic        b_l3ok;
logic [7:0]  b_reason;
logic        b_lookup;
logic        b_fin;
logic        b_rst;

always_comb begin
    b_fold = 17'(s2_sum_reg[15:0]) + 17'(s2_sum_reg[19:16]);
    b_fold = 17'(b_fold[15:0]) + 17'(b_fold[16]);
    b_csum_ok = b_fold[15:0] == 16'hffff;

    b_tcp = s2_proto_reg == 8'd6;
    b_l3ok = s2_is_ipv4_reg && s2_hdr_ok_reg && b_csum_ok;

    b_lookup = 1'b0;
    b_fin = 1'b0;
    b_rst = 1'b0;

    if (s2_bypass_reg) begin
        b_reason = RSN_BYPASS;
    end else if (s2_mcast_reg) begin
        b_reason = RSN_MCAST;
    end else if (!s2_is_ipv4_reg) begin
        b_reason = RSN_NOT_IPV4;
    end else if (!s2_hdr_ok_reg) begin
        b_reason = RSN_IP_HDR;
    end else if (!b_csum_ok) begin
        b_reason = RSN_CSUM;
    end else if (s2_frag_reg[13:0] != 0) begin
        b_reason = RSN_FRAG;
    end else if (s2_ttl_reg <= 8'd1) begin
        b_reason = RSN_TTL;
    end else if (s2_proto_reg != 8'd6 && s2_proto_reg != 8'd17) begin
        b_reason = RSN_PROTO;
    end else if (b_tcp ? s2_totlen_reg < 16'd40 : s2_totlen_reg < 16'd28) begin
        b_reason = RSN_IP_HDR;
    end else if (b_tcp && s2_tcp_flags_reg[1]) begin
        b_reason = RSN_SYN;
    end else if (b_tcp && (s2_tcp_flags_reg[0] || s2_tcp_flags_reg[2])) begin
        b_reason = RSN_FINRST;
        b_lookup = 1'b1;
        b_fin = s2_tcp_flags_reg[0];
        b_rst = s2_tcp_flags_reg[2];
    end else begin
        b_reason = RSN_MISS;
        b_lookup = 1'b1;
    end
end

// stage 3: descriptor output
logic  desc_valid_reg = 1'b0;
key_t  desc_key_reg = '0;
logic  desc_lookup_reg = 1'b0;
logic  desc_fin_reg = 1'b0;
logic  desc_rst_reg = 1'b0;
logic [15:0] desc_len_reg = '0;
meta_t desc_meta_reg = '0;

always_ff @(posedge clk) begin
    if (pipe_en) begin
        desc_valid_reg <= s2_valid_reg;

        desc_key_reg.lane <= 3'(LANE);
        desc_key_reg.vid <= s2_vlan_reg ? s2_tci_reg[11:0] : 12'd0;
        desc_key_reg.tcp <= b_tcp;
        desc_key_reg.sip <= s2_sip_reg;
        desc_key_reg.dip <= s2_dip_reg;
        desc_key_reg.sport <= s2_sport_reg;
        desc_key_reg.dport <= s2_dport_reg;

        desc_lookup_reg <= b_lookup;
        desc_fin_reg <= b_fin;
        desc_rst_reg <= b_rst;
        desc_len_reg <= s2_len_reg;

        desc_meta_reg.tci <= s2_tci_reg;
        desc_meta_reg.vlan <= s2_vlan_reg;
        desc_meta_reg.l3ok <= b_l3ok;
        desc_meta_reg.tcp <= b_tcp;
        desc_meta_reg.ttl <= s2_ttl_reg;
        desc_meta_reg.ip_csum <= s2_ip_csum_reg;
        desc_meta_reg.l4_csum <= s2_l4_csum_reg;
        desc_meta_reg.sip <= s2_sip_reg;
        desc_meta_reg.dip <= s2_dip_reg;
        desc_meta_reg.sport <= s2_sport_reg;
        desc_meta_reg.dport <= s2_dport_reg;
        desc_meta_reg.reason <= b_reason;
        desc_meta_reg.lookup <= b_lookup;
    end

    if (rst) begin
        desc_valid_reg <= 1'b0;
    end
end

assign m_desc_valid = desc_valid_reg;
assign m_desc_key = desc_key_reg;
assign m_desc_lookup = desc_lookup_reg;
assign m_desc_fin = desc_fin_reg;
assign m_desc_rst = desc_rst_reg;
assign m_desc_len = desc_len_reg;
assign m_desc_meta = desc_meta_reg;

endmodule

`resetall
