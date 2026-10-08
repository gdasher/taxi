// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal reference: the classification spec of natgw_model.parse(), re-stated
in SystemVerilog for use by the formal harnesses (not used in the design).
Frames are byte arrays of MAXB bytes; only bytes below 64 and below the frame
length are visible, as in the design.

*/

`default_nettype none

package natgw_formal_ref_pkg;

import natgw_pkg::*;

localparam MAXB = 80;

function automatic logic [7:0] rb(input logic [7:0] f[MAXB], input logic [6:0] l, input int i);
    return (i < 64 && i < int'(l)) ? f[i] : 8'd0;
endfunction

function automatic logic [15:0] rw(input logic [7:0] f[MAXB], input logic [6:0] l, input int i);
    return {rb(f, l, i), rb(f, l, i+1)};
endfunction

typedef struct packed {
    key_t       key;
    logic       lookup;
    logic       fin;
    logic       rst;
    logic [15:0] len;
    meta_t      meta;
} ref_t;

function automatic ref_t reference(input logic [7:0] f[MAXB], input logic [6:0] l, input logic byp, input logic [2:0] lane);
    ref_t r;
    logic [15:0] et, tci, totlen, frag, ipc, l4c;
    logic vlan, is_ipv4, hdr_ok, csum_ok, l3ok, tcp;
    logic [7:0] ver_ihl, ttl, proto, flags;
    logic [31:0] sip, dip, s;
    logic [15:0] sport, dport;
    logic [7:0] reason;
    int o, ip;

    et = rw(f, l, 12);
    vlan = 1'b0;
    tci = 16'd0;
    o = 0;
    if (et == 16'h8100) begin
        vlan = 1'b1;
        tci = rw(f, l, 14);
        et = rw(f, l, 16);
        o = 4;
    end
    ip = 14 + o;
    ver_ihl = rb(f, l, ip);
    totlen = rw(f, l, ip+2);
    frag = rw(f, l, ip+6);
    ttl = rb(f, l, ip+8);
    proto = rb(f, l, ip+9);
    ipc = rw(f, l, ip+10);
    sip = {rw(f, l, ip+12), rw(f, l, ip+14)};
    dip = {rw(f, l, ip+16), rw(f, l, ip+18)};
    sport = rw(f, l, ip+20);
    dport = rw(f, l, ip+22);
    flags = rb(f, l, ip+33);
    tcp = proto == 8'd6;
    l4c = tcp ? rw(f, l, ip+36) : rw(f, l, ip+26);

    s = 32'd0;
    for (int k = 0; k < 20; k += 2) s = s + 32'(rw(f, l, ip+k));
    s = 32'(s[15:0]) + 32'(s[31:16]);
    s = 32'(s[15:0]) + 32'(s[31:16]);
    csum_ok = s[15:0] == 16'hffff;

    is_ipv4 = et == 16'h0800;
    hdr_ok = ver_ihl == 8'h45 && int'(l) >= ip + 20 && totlen >= 16'd20 && ip + int'(totlen) <= int'(l);
    l3ok = is_ipv4 && hdr_ok && csum_ok;

    r = '0;
    r.fin = 1'b0;
    r.rst = 1'b0;
    r.lookup = 1'b0;
    if (byp) reason = RSN_BYPASS;
    else if (rb(f, l, 0) & 8'h01) reason = RSN_MCAST;
    else if (!is_ipv4) reason = RSN_NOT_IPV4;
    else if (!hdr_ok) reason = RSN_IP_HDR;
    else if (!csum_ok) reason = RSN_CSUM;
    else if (frag & 16'h3fff) reason = RSN_FRAG;
    else if (ttl <= 8'd1) reason = RSN_TTL;
    else if (proto != 8'd6 && proto != 8'd17) reason = RSN_PROTO;
    else if ((tcp && totlen < 16'd40) || (!tcp && totlen < 16'd28)) reason = RSN_IP_HDR;
    else if (tcp && flags[1]) reason = RSN_SYN;
    else if (tcp && (flags[0] || flags[2])) begin
        reason = RSN_FINRST;
        r.lookup = 1'b1;
        r.fin = flags[0];
        r.rst = flags[2];
    end else begin
        reason = RSN_MISS;
        r.lookup = 1'b1;
    end

    r.key.lane = lane;
    r.key.vid = vlan ? tci[11:0] : 12'd0;
    r.key.tcp = tcp;
    r.key.sip = sip;
    r.key.dip = dip;
    r.key.sport = sport;
    r.key.dport = dport;
    r.len = 16'(l);
    r.meta.tci = tci;
    r.meta.vlan = vlan;
    r.meta.l3ok = l3ok;
    r.meta.tcp = tcp;
    r.meta.ttl = ttl;
    r.meta.ip_csum = ipc;
    r.meta.l4_csum = l4c;
    r.meta.sip = sip;
    r.meta.dip = dip;
    r.meta.sport = sport;
    r.meta.dport = dport;
    r.meta.reason = reason;
    r.meta.lookup = r.lookup;
    return r;
endfunction


// one's-complement sum of 16-bit big-endian words of a byte array
function automatic logic [15:0] ocsum_bytes(input logic [7:0] f[MAXB], input int off, input int nwords);
    logic [31:0] s;
    s = 32'd0;
    for (int k = 0; k < nwords; k++) s = s + 32'({f[off + 2*k], f[off + 2*k + 1]});
    s = 32'(s[15:0]) + 32'(s[31:16]);
    s = 32'(s[15:0]) + 32'(s[31:16]);
    return s[15:0];
endfunction

// equality of one's-complement values (0x0000 and 0xFFFF both mean zero)
function automatic logic oc_eq(input logic [15:0] a, input logic [15:0] b);
    return a == b || ((a == 16'h0000 || a == 16'hffff) && (b == 16'h0000 || b == 16'hffff));
endfunction

endpackage
