// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: shared types, constants and checksum functions

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

package natgw_pkg;

    // Lanes
    localparam LANES = 8;
    localparam LANE_W = 3;

    // Punt reason codes (punt header byte 3)
    typedef enum logic [7:0] {
        RSN_MISS     = 8'd0,
        RSN_BYPASS   = 8'd1,
        RSN_NOT_IPV4 = 8'd2,
        RSN_MCAST    = 8'd3,
        RSN_IP_HDR   = 8'd4,
        RSN_FRAG     = 8'd5,
        RSN_TTL      = 8'd6,
        RSN_PROTO    = 8'd7,
        RSN_CSUM     = 8'd8,
        RSN_SYN      = 8'd9,
        RSN_FINRST   = 8'd10,
        RSN_VLAN     = 8'd11,
        RSN_NH       = 8'd12,
        RSN_FWD      = 8'd15     // not a punt: forwarded by hardware (statistics index only)
    } reason_t;

    localparam RSN_CNT = 16;

    // Lookup key: 112 bits
    typedef struct packed {
        logic [15:0] dport;
        logic [15:0] sport;
        logic [31:0] dip;
        logic [31:0] sip;
        logic        tcp;     // 1 = TCP, 0 = UDP
        logic [11:0] vid;     // 0 when untagged
        logic [2:0]  lane;    // ingress lane
    } key_t;

    localparam KEY_W = $bits(key_t);

    // Table entry: 216 bits (three URAMs wide)
    typedef struct packed {
        logic [42:0] rsvd;
        logic [9:0]  nh_idx;
        logic        dec_ttl;
        logic [15:0] new_port;
        logic [31:0] new_ip;
        logic        xlate_dst; // 0 = rewrite source (SNAT), 1 = rewrite destination (DNAT)
        key_t        key;
        logic        valid;
    } entry_t;

    localparam ENTRY_W = $bits(entry_t);

    // Next-hop entry: 113 bits used, 128 stored
    typedef struct packed {
        logic [14:0] rsvd;
        logic [11:0] vid;
        logic        vlan;
        logic [2:0]  lane;
        logic [47:0] src_mac;
        logic [47:0] dst_mac;
        logic        valid;
    } nh_t;

    localparam NH_W = $bits(nh_t);
    localparam NH_IDX_W = 10;

    // Per-entry state: 144 bits (two URAMs wide)
    typedef struct packed {
        logic [55:0] bytes;
        logic [47:0] pkts;
        logic [31:0] ts;
        logic [2:0]  rsvd;
        logic        tcp;
        logic        evp;     // idle event pending
        logic        rst;     // RST seen
        logic        fin;     // FIN seen
        logic        valid;
    } state_t;

    localparam STATE_W = $bits(state_t);

    // Event types
    localparam logic [3:0] EVT_IDLE = 4'd1;
    localparam logic [3:0] EVT_FIN  = 4'd2;
    localparam logic [3:0] EVT_RST  = 4'd3;
    localparam logic [3:0] EVT_OVF  = 4'd15;

    // Per-frame metadata from the parser, consumed by the rewrite stage
    typedef struct packed {
        logic [15:0] tci;      // VLAN TCI (0 when untagged)
        logic        vlan;
        logic        l3ok;     // well-formed IPv4 with good header checksum
        logic        tcp;
        logic [7:0]  ttl;
        logic [15:0] ip_csum;
        logic [15:0] l4_csum;
        logic [31:0] sip;
        logic [31:0] dip;
        logic [15:0] sport;
        logic [15:0] dport;
        logic [7:0]  reason;   // pre-lookup reason (RSN_MISS = needs lookup / no exception)
        logic        lookup;   // key was looked up (MISS or FINRST)
    } meta_t;

    localparam META_W = $bits(meta_t);

    // Lookup result, per frame, in order per lane
    typedef struct packed {
        logic [31:0] hash;     // h0
        nh_t         nh;
        logic [9:0]  nh_idx;
        logic        dec_ttl;
        logic [15:0] new_port;
        logic [31:0] new_ip;
        logic        xlate_dst;
        logic [31:0] idx;      // entry index (zero-extended)
        logic        hit;
    } result_t;

    localparam RESULT_W = $bits(result_t);

    // Punt header
    localparam logic [15:0] PUNT_MAGIC = 16'h4E47;
    localparam logic [7:0] PUNT_VER = 8'd1;

    // Hash polynomials (reflected)
    localparam logic [31:0] POLY_CRC32C = 32'h82F63B78;
    localparam logic [31:0] POLY_CRC32  = 32'hEDB88320;

    // Reflected CRC over the key, bit 0 first, starting from seed, no final XOR
    function automatic logic [31:0] key_crc(input logic [KEY_W-1:0] key, input logic [31:0] seed, input logic [31:0] poly);
        logic [31:0] crc;
        crc = seed;
        for (int i = 0; i < KEY_W; i++) begin
            if (crc[0] ^ key[i])
                crc = (crc >> 1) ^ poly;
            else
                crc = crc >> 1;
        end
        return crc;
    endfunction

    // One's complement 16-bit add
    function automatic logic [15:0] oc_add(input logic [15:0] a, input logic [15:0] b);
        logic [16:0] s;
        s = {1'b0, a} + {1'b0, b};
        return s[15:0] + 16'(s[16]);
    endfunction

    // RFC 1624 incremental update of a checksum for up to three changed 16-bit words:
    // HC' = ~(~HC + ~m0 + m0' + ~m1 + m1' + ~m2 + m2')
    function automatic logic [15:0] csum_update3(
        input logic [15:0] hc,
        input logic [15:0] m0, input logic [15:0] n0,
        input logic [15:0] m1, input logic [15:0] n1,
        input logic [15:0] m2, input logic [15:0] n2
    );
        logic [15:0] nhc, nm0, nm1, nm2;
        logic [19:0] s;
        logic [16:0] f;
        // invert at 16 bits before widening
        nhc = ~hc;
        nm0 = ~m0;
        nm1 = ~m1;
        nm2 = ~m2;
        s = 20'(nhc) + 20'(nm0) + 20'(n0) + 20'(nm1) + 20'(n1) + 20'(nm2) + 20'(n2);
        f = 17'(s[15:0]) + 17'(s[19:16]);
        f = 17'(f[15:0]) + 17'(f[16]);
        return ~f[15:0];
    endfunction

endpackage

`resetall
