// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: natgw_rewrite

Two frames of arbitrary content and length (1..80 bytes) enter back to back,
each with the metadata the parser produces for it (the spec reference) and an
arbitrary lookup result (consistent with the engine: no hit and hash 0 when
the frame is not looked up). Configuration is arbitrary; every interface
stalls arbitrarily. Each output frame is captured and checked:

  P3.1  routing: a frame is forwarded iff its reason is MISS, the lookup hit,
        the next hop is valid, its egress lane is enabled and its VLAN tagging
        matches; frames leave each output in input order; stat_reason reports
        the spec reason (FWD when forwarded), once per frame, in order
  P3.2  punted frames are byte-for-byte unchanged, with tuser preserved, after
        a punt header with exactly the specified fields when enabled (never
        for BYPASS)
  P3.3  forwarded frames: same length; tdest = next-hop lane; MACs, VLAN VID
        (PCP/DEI kept), TTL and the translated address and port are the
        specified values; every other byte is unchanged
  P3.4  forwarded frames carry a valid IPv4 header checksum
  P3.5  the TCP/UDP checksum stays consistent for any payload: the one's-
        complement sum over the words that change (addresses, ports, checksum)
        is unchanged; a UDP checksum of 0 stays 0 and is never produced
        otherwise

*/

`default_nettype none

module formal_rewrite
    import natgw_pkg::*;
    import natgw_formal_ref_pkg::*;
#(
    parameter LANE = 2,
    parameter logic NOSTALL = 1'b0     // 1: no idle cycles, no backpressure, frames up to 64 bytes
)
(
    input wire logic clk,
    input wire logic in_go,
    input wire logic res_go,
    input wire logic meta_go,
    input wire logic fwd_ready,
    input wire logic punt_ready
);

logic [7:0] frame[2][MAXB];
logic [6:0] len[2];
logic [1:0] user[2];
result_t    res_in[2];
logic       bypass[2];
logic       cfg_punt_hdr;
logic [7:0] cfg_egress_en;

// arbitrary constants: no initial value, held for the whole trace
// (the slang frontend does not honour (* anyconst *))
always_ff @(posedge clk) begin
    frame <= frame;
    len <= len;
    user <= user;
    res_in <= res_in;
    bypass <= bypass;
    cfg_punt_hdr <= cfg_punt_hdr;
    cfg_egress_en <= cfg_egress_en;
end

logic init = 1'b1;
always_ff @(posedge clk) init <= 1'b0;
wire rst = init;

ref_t r[2];
assign r[0] = reference(frame[0], len[0], bypass[0], 3'(LANE));
assign r[1] = reference(frame[1], len[1], bypass[1], 3'(LANE));

always_comb begin
    if (NOSTALL) begin
        assume (in_go && res_go && meta_go && fwd_ready && punt_ready);
    end
    for (int x = 0; x < 2; x++) begin
        assume (len[x] >= 1 && len[x] <= (NOSTALL ? 64 : MAXB));
        if (!r[x].lookup) assume (!res_in[x].hit && res_in[x].hash == 32'd0);
    end
end

// spec decision per frame: {forward, reason}
function automatic logic [8:0] decide(input ref_t rr, input result_t rs, input logic [7:0] egress_en);
    if (rr.meta.reason == RSN_MISS && rs.hit) begin
        if (!rs.nh.valid || !egress_en[rs.nh.lane]) return {1'b0, RSN_NH};
        if (rs.nh.vlan != rr.meta.vlan) return {1'b0, RSN_VLAN};
        return {1'b1, RSN_FWD};
    end
    return {1'b0, rr.meta.reason};
endfunction

wire [8:0] dec0 = decide(r[0], res_in[0], cfg_egress_en);
wire [8:0] dec1 = decide(r[1], res_in[1], cfg_egress_en);
wire       fwd[2];
wire [7:0] rsn[2];
assign fwd[0] = dec0[8];
assign fwd[1] = dec1[8];
assign rsn[0] = dec0[7:0];
assign rsn[1] = dec1[7:0];

// ---------------------------------------------------------------- sources

taxi_axis_if #(.DATA_W(128), .USER_EN(1), .USER_W(2)) hold_axis(), punt_axis();
taxi_axis_if #(.DATA_W(128), .DEST_EN(1), .DEST_W(3), .USER_EN(1), .USER_W(1)) fwd_axis();

function automatic logic [127:0] beat_data(input logic [7:0] f[MAXB], input int b);
    logic [127:0] d;
    for (int k = 0; k < 16; k++) d[8*k +: 8] = (16*b + k < MAXB) ? f[16*b + k] : 8'd0;
    return d;
endfunction

function automatic logic [15:0] beat_keep(input logic [6:0] l, input int b);
    logic [15:0] m;
    for (int k = 0; k < 16; k++) m[k] = 16*b + k < int'(l);
    return m;
endfunction

logic       src_x = 1'b0;
logic [2:0] src_beat = '0;
logic       src_done = 1'b0;
logic       src_valid = 1'b0;
wire        src_last = 16*(int'(src_beat)+1) >= int'(len[src_x]);

assign hold_axis.tdata = beat_data(frame[src_x], int'(src_beat));
assign hold_axis.tkeep = beat_keep(len[src_x], int'(src_beat));
assign hold_axis.tstrb = hold_axis.tkeep;
assign hold_axis.tvalid = src_valid;
assign hold_axis.tlast = src_last;
assign hold_axis.tid = '0;
assign hold_axis.tdest = '0;
assign hold_axis.tuser = user[src_x];

always_ff @(posedge clk) begin
    if (src_valid && hold_axis.tready) begin
        src_valid <= 1'b0;
        if (src_last) begin
            src_beat <= '0;
            if (src_x) src_done <= 1'b1;
            src_x <= 1'b1;
        end else begin
            src_beat <= src_beat + 1;
        end
    end
    if ((!src_valid || hold_axis.tready) && in_go && !src_done && !(src_valid && hold_axis.tready && src_last && src_x)) begin
        src_valid <= 1'b1;
    end
    if (rst) begin
        src_x <= 1'b0;
        src_beat <= '0;
        src_done <= 1'b0;
        src_valid <= 1'b0;
    end
end

// result and meta streams (independent pacing)
logic [1:0] res_n = '0, meta_n = '0;
logic res_v = 1'b0, meta_v = 1'b0;
wire res_ready, meta_ready;

always_ff @(posedge clk) begin
    if (res_v && res_ready) begin
        res_v <= 1'b0;
        res_n <= res_n + 1;
    end
    if ((!res_v || res_ready) && res_go && (res_n + 2'(res_v && res_ready)) < 2'd2) res_v <= 1'b1;
    if (meta_v && meta_ready) begin
        meta_v <= 1'b0;
        meta_n <= meta_n + 1;
    end
    if ((!meta_v || meta_ready) && meta_go && (meta_n + 2'(meta_v && meta_ready)) < 2'd2) meta_v <= 1'b1;
    if (rst) begin
        res_n <= '0;
        meta_n <= '0;
        res_v <= 1'b0;
        meta_v <= 1'b0;
    end
end

wire stat_valid;
wire [7:0] stat_reason;

natgw_rewrite #(
    .LANE(LANE),
    .USER_W(2)
)
dut (
    .clk(clk),
    .rst(rst),
    .s_axis_hold(hold_axis),
    .s_res_valid(res_v),
    .s_res_ready(res_ready),
    .s_res(res_in[res_n[0]]),
    .s_meta_valid(meta_v),
    .s_meta_ready(meta_ready),
    .s_meta(r[meta_n[0]].meta),
    .m_axis_fwd(fwd_axis),
    .m_axis_punt(punt_axis),
    .cfg_punt_hdr(cfg_punt_hdr),
    .cfg_egress_en(cfg_egress_en),
    .stat_valid(stat_valid),
    .stat_reason(stat_reason)
);

assign fwd_axis.tready = fwd_ready;
assign punt_axis.tready = punt_ready;

// ---------------------------------------------------------------- P3.1 statistics order

logic [1:0] stat_n = '0;
always_ff @(posedge clk) begin
    if (!rst && stat_valid) begin
        assert (stat_n < 2'd2);
        assert (stat_reason == rsn[stat_n[0]]);
        stat_n <= stat_n + 1;
    end
    if (rst) stat_n <= '0;
end

// ---------------------------------------------------------------- output capture

// which input frame each output carries next: forwarded frames in input order
// on the forward output, punted frames in input order on the punt output
logic [1:0] fwd_n = '0, punt_n = '0;
wire fwd_x = (fwd_n == 2'd0) ? !fwd[0] : 1'b1;    // first forwarded frame: a if a is forwarded, else b
wire punt_x = (punt_n == 2'd0) ? fwd[0] : 1'b1;

logic [7:0] cap_f[96];
logic [6:0] cap_f_len = '0;
logic [7:0] cap_p[96];
logic [6:0] cap_p_len = '0;

// punt header
function automatic logic [7:0] hdr_byte(input int x, input int i);
    logic [127:0] h;
    logic [7:0] flags;
    logic [31:0] idx;
    flags = {4'd0, (res_in[x].hit && r[x].lookup), r[x].meta.vlan, 1'b0, r[x].meta.l3ok};
    idx = (res_in[x].hit && r[x].lookup) ? res_in[x].idx : 32'hffffffff;
    h = {idx[7:0], idx[15:8], idx[23:16], idx[31:24],
         res_in[x].hash[7:0], res_in[x].hash[15:8], res_in[x].hash[23:16], res_in[x].hash[31:24],
         r[x].meta.tci[7:0], r[x].meta.tci[15:8], flags, 8'(LANE), rsn[x], PUNT_VER, PUNT_MAGIC[7:0], PUNT_MAGIC[15:8]};
    return h[8*i +: 8];
endfunction


always_ff @(posedge clk) begin
    // forward output
    if (!rst && fwd_axis.tvalid && fwd_axis.tready) begin
        assert (fwd_n < 2'd2);
        assert (fwd[fwd_x]);
        assert (fwd_axis.tuser == 1'b0);
        assert (fwd_axis.tdest == res_in[fwd_x].nh.lane);
        for (int k = 0; k < 16; k++) begin
            if (fwd_axis.tkeep[k] && int'(cap_f_len) + k < 96) cap_f[int'(cap_f_len) + k] <= fwd_axis.tdata[8*k +: 8];
        end
        cap_f_len <= cap_f_len + 7'($countones(fwd_axis.tkeep));
        if (fwd_axis.tlast) begin
            cap_f_len <= '0;
            fwd_n <= fwd_n + 1;
        end
    end
    // punt output
    if (!rst && punt_axis.tvalid && punt_axis.tready) begin
        assert (punt_n < 2'd2);
        assert (!fwd[punt_x]);
        assert (punt_axis.tuser == user[punt_x]);
        for (int k = 0; k < 16; k++) begin
            if (punt_axis.tkeep[k] && int'(cap_p_len) + k < 96) cap_p[int'(cap_p_len) + k] <= punt_axis.tdata[8*k +: 8];
        end
        cap_p_len <= cap_p_len + 7'($countones(punt_axis.tkeep));
        if (punt_axis.tlast) begin
            cap_p_len <= '0;
            punt_n <= punt_n + 1;
        end
    end
    if (rst) begin
        fwd_n <= '0;
        punt_n <= '0;
        cap_f_len <= '0;
        cap_p_len <= '0;
    end
end

// frame-complete checks, one cycle after the last beat is captured
logic       f_done = 1'b0, p_done = 1'b0;
logic       f_x, p_x;
logic [6:0] f_len, p_len;
always_ff @(posedge clk) begin
    f_done <= !rst && fwd_axis.tvalid && fwd_axis.tready && fwd_axis.tlast;
    f_x <= fwd_x;
    f_len <= cap_f_len + 7'($countones(fwd_axis.tkeep));
    p_done <= !rst && punt_axis.tvalid && punt_axis.tready && punt_axis.tlast;
    p_x <= punt_x;
    p_len <= cap_p_len + 7'($countones(punt_axis.tkeep));
end

// P3.2 punted frames
always_comb begin
    if (!rst && p_done) begin
        automatic logic hdr = cfg_punt_hdr && rsn[p_x] != RSN_BYPASS;
        automatic int h = hdr ? 16 : 0;
        assert (int'(p_len) == int'(len[p_x]) + h);
        for (int i = 0; i < 16; i++) if (hdr) assert (cap_p[i] == hdr_byte(int'(p_x), i));
        for (int i = 0; i < MAXB; i++) if (i < int'(len[p_x])) assert (cap_p[i + h] == frame[p_x][i]);
    end
end

// P3.3 - P3.5 forwarded frames
always_comb begin
    if (!rst && f_done) begin
        automatic int x = int'(f_x);
        automatic logic vlan = r[x].meta.vlan;
        automatic int ip = vlan ? 18 : 14;
        automatic logic dst = res_in[x].xlate_dst;
        automatic logic tcp = r[x].meta.tcp;
        automatic int l4c = tcp ? ip + 36 : ip + 26;
        automatic logic [7:0] o[MAXB];
        automatic logic [15:0] old_s, new_s;
        for (int i = 0; i < MAXB; i++) o[i] = cap_f[i];

        assert (f_len == len[x]);
        // P3.3 specified fields
        assert ({o[0], o[1], o[2], o[3], o[4], o[5]} == res_in[x].nh.dst_mac);
        assert ({o[6], o[7], o[8], o[9], o[10], o[11]} == res_in[x].nh.src_mac);
        if (vlan) assert ({o[14], o[15]} == {r[x].meta.tci[15:12], res_in[x].nh.vid});
        assert (o[ip+8] == (res_in[x].dec_ttl ? r[x].meta.ttl - 8'd1 : r[x].meta.ttl));
        if (dst) begin
            assert ({o[ip+16], o[ip+17], o[ip+18], o[ip+19]} == res_in[x].new_ip);
            assert ({o[ip+22], o[ip+23]} == res_in[x].new_port);
        end else begin
            assert ({o[ip+12], o[ip+13], o[ip+14], o[ip+15]} == res_in[x].new_ip);
            assert ({o[ip+20], o[ip+21]} == res_in[x].new_port);
        end
        // P3.3 everything else unchanged
        for (int i = 12; i < MAXB; i++) begin
            if (i < int'(len[x])
                    && !(vlan && (i == 14 || i == 15))
                    && i != ip+8 && i != ip+10 && i != ip+11
                    && !(dst && i >= ip+16 && i <= ip+19) && !(!dst && i >= ip+12 && i <= ip+15)
                    && !(dst && (i == ip+22 || i == ip+23)) && !(!dst && (i == ip+20 || i == ip+21))
                    && i != l4c && i != l4c+1) begin
                assert (o[i] == frame[x][i]);
            end
        end
        // P3.4 IPv4 header checksum
        assert (ocsum_bytes(o, ip, 10) == 16'hffff);
        // P3.5 TCP/UDP checksum consistency
        old_s = ocsum_bytes(frame[x], ip+12, 6) + 16'd0;
        if (!tcp && {frame[x][l4c], frame[x][l4c+1]} == 16'd0) begin
            assert ({o[l4c], o[l4c+1]} == 16'd0);
        end else begin
            automatic logic [31:0] so, sn;
            so = 32'(ocsum_bytes(frame[x], ip+12, 6)) + 32'({frame[x][l4c], frame[x][l4c+1]});
            sn = 32'(ocsum_bytes(o, ip+12, 6)) + 32'({o[l4c], o[l4c+1]});
            so = 32'(so[15:0]) + 32'(so[31:16]);
            sn = 32'(sn[15:0]) + 32'(sn[31:16]);
            assert (oc_eq(so[15:0], sn[15:0]));
            if (!tcp) assert ({o[l4c], o[l4c+1]} != 16'd0);
        end
    end
end

always_ff @(posedge clk) begin
    if (!rst) cover (f_done);
    if (!rst) cover (p_done && cfg_punt_hdr);
    if (!rst) cover (fwd_n == 2'd1 && punt_n == 2'd1);
end

endmodule
