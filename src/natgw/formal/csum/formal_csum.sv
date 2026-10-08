// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

Formal: RFC 1624 incremental checksum update (natgw_pkg::csum_update3,
csum_sum3, csum_fold), checked against an independent one's-complement sum.

The message is N 16-bit words plus a checksum word. Words 0..2 are replaced
(the rewrite changes at most three words per checksum: two IP-address words
and the TTL/protocol word, or two IP-address words and a port). All inputs
are free, so every property is proved for all values (combinational).

*/

`default_nettype none

module formal_csum
    import natgw_pkg::*;
#(
    parameter N = 8
)
(
    input wire logic [15:0] w[N],     // message words (checksum excluded)
    input wire logic [15:0] n[3],     // new values for words 0..2
    input wire logic [15:0] hc        // checksum carried in the message
);

// one's-complement sum, written independently of natgw_pkg
function automatic logic [15:0] ocsum(input logic [15:0] v[N], input logic [15:0] c);
    logic [31:0] s;
    s = 32'(c);
    for (int i = 0; i < N; i++) s = s + 32'(v[i]);
    s = 32'(s[15:0]) + 32'(s[31:16]);
    s = 32'(s[15:0]) + 32'(s[31:16]);
    return s[15:0];
endfunction

logic [15:0] w2[N];
always_comb begin
    for (int i = 0; i < N; i++) w2[i] = w[i];
    w2[0] = n[0];
    w2[1] = n[1];
    w2[2] = n[2];
end

wire [15:0] hc2 = csum_update3(hc, w[0], n[0], w[1], n[1], w[2], n[2]);
wire [15:0] full = ~ocsum(w2, 16'd0);   // full recompute over the new message

// a real header always has a non-zero word that is not rewritten
// (IPv4: version/IHL; TCP/UDP pseudo header: protocol)
wire anchor = w[3] != 16'd0;

always_comb begin
    if (anchor) begin
        // P1.1 valid in, valid out
        if (ocsum(w, hc) == 16'hffff) assert (ocsum(w2, hc2) == 16'hffff);
        // P1.2 a bad checksum stays bad (receiver still drops the packet)
        if (ocsum(w, hc) != 16'hffff) assert (ocsum(w2, hc2) != 16'hffff);
        // P1.3 the incremental result equals a full recompute, up to the two encodings of zero
        if (ocsum(w, hc) == 16'hffff) assert (hc2 == full || (hc2 | full) == 16'hffff && (hc2 & full) == 16'h0000);
        // P1.4 UDP: replacing a computed 0x0000 by 0xFFFF keeps the packet valid
        if (ocsum(w, hc) == 16'hffff && hc2 == 16'h0000) assert (ocsum(w2, 16'hffff) == 16'hffff);
    end
    // P1.5 csum_update3 is exactly csum_fold(csum_sum3(...)) (the split used in natgw_rewrite)
    assert (hc2 == csum_fold(csum_sum3(hc, w[0], n[0], w[1], n[1], w[2], n[2])));
end

endmodule
