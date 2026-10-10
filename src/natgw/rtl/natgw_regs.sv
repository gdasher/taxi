// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: control and status registers (AXI-Lite, BAR0 + 0x800000)

Register map (byte offsets, 32-bit registers):

0x0000  ID            RO  0x4E415447 ("NATG")
0x0004  VERSION       RO  0x00010100 (1.1: DDR tier registers)
0x0008  CAPS          RO  [7:0] IDX_W, [15:8] lanes, [23:16] punt header bytes
0x000C  SCRATCH       RW
0x0010  CTRL          RW  [0] enable, [1] punt header, [15:8] bypass mask (reset 0xFF),
                          [23:16] egress enable (reset 0xFF)
0x0014  CLEAR         W: [0] start table + state clear; R: [0] busy
0x0020  SEED0         RW  reset 0xFFFFFFFF
0x0024  SEED1         RW  reset 0xFFFFFFFF
0x0028  TICK_DIV      RW  clock cycles per tick (reset 250000 = 1 ms)
0x002C  TICK          RO  current tick
0x0030  THRESH_TCP    RW  idle threshold, ticks (reset 7440000)
0x0034  THRESH_UDP    RW  idle threshold, ticks (reset 300000)
0x0038  SCAN          RW  [31] enable (reset 1), [15:0] interval in cycles (reset 5)
0x003C  BUBBLE        RW  [15:0] cycles between forced idle slots (reset 16, 0 = never)
0x0060  DDR_STATUS    RO  [0] DDR tier in this build, [1] memory calibrated (a DIMM is
                          fitted and working), [2] enabled, [3] clearing, [4] active
                          (looked up), [15:8] DDR_BUCKET_W, [23:16] lookups in flight
                          per lane (cap); all zero without a DDR tier
0x0064  DDR_CTRL      RW  [0] enable; W [1] start clearing the DDR table and bitmap
0x0068  DDR_LOOKUPS   RO  DDR lookups issued (wraps)
0x006C  DDR_HITS      RO  DDR hits (wraps)
0x0070  DDR_SKIPS     RO  misses not looked up in DDR: request cap reached (wraps)
0x0100  ENT_DATA0..6  RW  entry, 216 bits (word 0 = bits 31:0)
0x0120  ST_DATA0..4   RW  state, 144 bits
0x0140  INDEX         RW  entry index
0x0144  CMD           W:  1 write entry (and init its state), 2 write state, 3 read entry,
                          4 read state, 5 clear entry (entry and state zeroed),
                          6 write DDR entry (ENT_DATA at DDR index INDEX), 7 clear DDR entry,
                          8 read DDR entry into ENT_DATA, 9 read and clear activity word INDEX
                      R:  [0] busy
0x0148  ACT_LO        RO  activity bits 31:0 of the last word read (DDR entries 64*INDEX + n)
0x014C  ACT_HI        RO  activity bits 63:32
0x0200  NH_INDEX      RW
0x0204  NH_CMD        W:  1 write, 2 read; R: [0] busy
0x0210  NH_DATA0..3   RW  next hop, 128 bits
0x0300  EVT_STATUS    RO  [0] event available
0x0304  EVT_LO        RO  head event bits 31:0 (tick)
0x0308  EVT_HI        RO  head event bits 63:32; reading pops the event
0x0310  EVT_DROPS     RO  FIN/RST events lost to a full FIFO
0x1000 + lane*0x100 + n*8   statistics, 64-bit (read LO first: it latches HI)
        n = 0..15: frames by reason (15 = forwarded)
        n = 16: ingress FIFO overflow drops, n = 17: bad-FCS drops

Writes and reads stall while an indirect command is in progress, so posted
command sequences need no polling. Command data is captured when the command
is accepted.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_regs
    import natgw_pkg::*;
#(
    parameter IDX_W = 19,
    parameter TICK_DIV_RST = 250000,
    parameter DDR_ENABLE = 0,
    parameter DDR_BUCKET_W = 20,
    parameter DDR_MAX_OUT = 16,
    parameter DIDX_W = DDR_BUCKET_W + 2
)
(
    input  wire logic              clk,
    input  wire logic              rst,

    taxi_axil_if.wr_slv            s_axil_wr,
    taxi_axil_if.rd_slv            s_axil_rd,

    // configuration
    output wire logic              cfg_enable,
    output wire logic              cfg_punt_hdr,
    output wire logic [LANES-1:0]  cfg_bypass,
    output wire logic [LANES-1:0]  cfg_egress_en,
    output wire logic [31:0]       cfg_seed0,
    output wire logic [31:0]       cfg_seed1,
    output wire logic [31:0]       cfg_tick,
    output wire logic [31:0]       cfg_thresh_tcp,
    output wire logic [31:0]       cfg_thresh_udp,
    output wire logic              cfg_scan_en,
    output wire logic [15:0]       cfg_scan_interval,
    output wire logic [15:0]       cfg_bubble_period,

    output wire logic              clear_start,
    input  wire logic              clear_busy,

    // entry table access
    output wire logic              host_ent_valid,
    input  wire logic              host_ent_ready,
    output wire logic              host_ent_we,
    output wire logic [IDX_W-1:0]  host_ent_idx,
    output wire entry_t            host_ent_wdata,
    input  wire logic              host_ent_rvalid,
    input  wire entry_t            host_ent_rdata,

    // state access
    output wire logic              host_st_valid,
    input  wire logic              host_st_ready,
    output wire logic              host_st_we,
    output wire logic [IDX_W-1:0]  host_st_idx,
    output wire state_t            host_st_wdata,
    input  wire logic              host_st_rvalid,
    input  wire state_t            host_st_rdata,

    // next-hop access
    output wire logic              host_nh_valid,
    input  wire logic              host_nh_ready,
    output wire logic              host_nh_we,
    output wire logic [9:0]        host_nh_idx,
    output wire nh_t               host_nh_wdata,
    input  wire logic              host_nh_rvalid,
    input  wire nh_t               host_nh_rdata,

    // events
    input  wire logic              s_evt_valid,
    output wire logic              s_evt_ready,
    input  wire logic [63:0]       s_evt,

    // statistics inputs
    input  wire logic              stat_valid[LANES],
    input  wire logic [7:0]        stat_reason[LANES],
    input  wire logic              stat_ingress_drop[LANES],
    input  wire logic              stat_ingress_bad[LANES],
    input  wire logic              stat_evt_drop,

    // DDR tier (unused without one)
    input  wire logic              ddr_calib,
    input  wire logic              ddr_active,
    output wire logic              cfg_ddr_en,
    output wire logic              ddr_clear_start,
    input  wire logic              ddr_clear_busy,
    output wire logic              host_ddr_valid,
    input  wire logic              host_ddr_ready,
    output wire logic [1:0]        host_ddr_op,
    output wire logic [DIDX_W-1:0] host_ddr_idx,
    output wire entry_t            host_ddr_wdata,
    input  wire logic              host_ddr_done,
    input  wire entry_t            host_ddr_rdata,
    output wire logic              act_valid,
    input  wire logic              act_ready,
    output wire logic [DIDX_W-7:0] act_word,
    input  wire logic              act_rvalid,
    input  wire logic [63:0]       act_rdata,
    input  wire logic              stat_ddr_lookup,
    input  wire logic              stat_ddr_hit,
    input  wire logic              stat_ddr_skip
);

localparam STAT_N = 18;

// configuration registers
logic        enable_reg = 1'b0;
logic        punt_hdr_reg = 1'b0;
logic [7:0]  bypass_reg = 8'hff;
logic [7:0]  egress_en_reg = 8'hff;
logic [31:0] scratch_reg = '0;
logic [31:0] seed0_reg = '1;
logic [31:0] seed1_reg = '1;
logic [31:0] tick_div_reg = TICK_DIV_RST;
logic [31:0] tick_cnt_reg = '0;
logic [31:0] tick_reg = '0;
logic [31:0] thresh_tcp_reg = 32'd7440000;
logic [31:0] thresh_udp_reg = 32'd300000;
logic        scan_en_reg = 1'b1;
logic [15:0] scan_interval_reg = 16'd5;
logic [15:0] bubble_reg = 16'd16;
logic        clear_start_reg = 1'b0;
logic        init_clear_reg = 1'b1;

assign cfg_enable = enable_reg;
assign cfg_punt_hdr = punt_hdr_reg;
assign cfg_bypass = bypass_reg | {LANES{!enable_reg}};
assign cfg_egress_en = egress_en_reg;
assign cfg_seed0 = seed0_reg;
assign cfg_seed1 = seed1_reg;
assign cfg_tick = tick_reg;
assign cfg_thresh_tcp = thresh_tcp_reg;
assign cfg_thresh_udp = thresh_udp_reg;
assign cfg_scan_en = scan_en_reg;
assign cfg_scan_interval = scan_interval_reg;
assign cfg_bubble_period = bubble_reg;
assign clear_start = clear_start_reg;

// indirect access data
logic [31:0] ent_data_reg[7];
logic [31:0] st_data_reg[5];
logic [31:0] index_reg = '0;
logic [31:0] nh_data_reg[4];
logic [9:0]  nh_index_reg = '0;

// command engine
typedef enum logic [3:0] {
    CMD_NONE = 4'd0,
    CMD_WR_ENT = 4'd1,
    CMD_WR_ST = 4'd2,
    CMD_RD_ENT = 4'd3,
    CMD_RD_ST = 4'd4,
    CMD_CLR = 4'd5,
    CMD_DDR_WR = 4'd6,
    CMD_DDR_CLR = 4'd7,
    CMD_DDR_RD = 4'd8,
    CMD_ACT_RC = 4'd9
} cmd_t;

logic        ent_pend_reg = 1'b0;
logic        ent_we_reg = 1'b0;
logic        ent_wait_reg = 1'b0;
entry_t      ent_wdata_reg = '0;
logic        st_pend_reg = 1'b0;
logic        st_we_reg = 1'b0;
logic        st_wait_reg = 1'b0;
state_t      st_wdata_reg = '0;
logic [IDX_W-1:0] cmd_idx_reg = '0;
logic        nh_pend_reg = 1'b0;
logic        nh_we_reg = 1'b0;
logic        nh_wait_reg = 1'b0;
nh_t         nh_wdata_reg = '0;

// DDR tier
logic        ddr_en_reg = 1'b0;
logic        ddr_clear_start_reg = 1'b0;
logic        ddr_pend_reg = 1'b0;
logic        ddr_wait_reg = 1'b0;
logic [1:0]  ddr_op_reg = '0;
entry_t      ddr_wdata_reg = '0;
logic        act_pend_reg = 1'b0;
logic        act_wait_reg = 1'b0;
logic [31:0] act_data_reg[2];
logic [31:0] ddr_lookups_reg = '0;
logic [31:0] ddr_hits_reg = '0;
logic [31:0] ddr_skips_reg = '0;

assign cfg_ddr_en = ddr_en_reg && DDR_ENABLE != 0;
assign ddr_clear_start = ddr_clear_start_reg;
assign host_ddr_valid = ddr_pend_reg;
assign host_ddr_op = ddr_op_reg;
assign host_ddr_idx = DIDX_W'(index_reg);
assign host_ddr_wdata = ddr_wdata_reg;
assign act_valid = act_pend_reg;
assign act_word = (DIDX_W-6)'(index_reg);

wire busy = ent_pend_reg || ent_wait_reg || st_pend_reg || st_wait_reg || nh_pend_reg || nh_wait_reg ||
    ddr_pend_reg || ddr_wait_reg || act_pend_reg || act_wait_reg;

assign host_ent_valid = ent_pend_reg;
assign host_ent_we = ent_we_reg;
assign host_ent_idx = cmd_idx_reg;
assign host_ent_wdata = ent_wdata_reg;
assign host_st_valid = st_pend_reg;
assign host_st_we = st_we_reg;
assign host_st_idx = cmd_idx_reg;
assign host_st_wdata = st_wdata_reg;
assign host_nh_valid = nh_pend_reg;
assign host_nh_we = nh_we_reg;
assign host_nh_idx = nh_index_reg;
assign host_nh_wdata = nh_wdata_reg;

// statistics
logic [63:0] stat_cnt_reg[LANES][STAT_N];
logic [31:0] stat_hi_shadow_reg = '0;
logic [31:0] evt_drop_reg = '0;

for (genvar l = 0; l < LANES; l = l + 1) begin : stat_lane
    // register the increments to keep the counter adders off the input paths
    logic       valid_reg = 1'b0;
    logic [3:0] reason_reg = '0;
    logic       drop_reg = 1'b0;
    logic       bad_reg = 1'b0;

    always_ff @(posedge clk) begin
        valid_reg <= stat_valid[l];
        reason_reg <= stat_reason[l][3:0];
        drop_reg <= stat_ingress_drop[l];
        bad_reg <= stat_ingress_bad[l];

        for (int n = 0; n < 16; n++) begin
            if (valid_reg && reason_reg == 4'(n)) begin
                stat_cnt_reg[l][n] <= stat_cnt_reg[l][n] + 1;
            end
        end
        if (drop_reg) begin
            stat_cnt_reg[l][16] <= stat_cnt_reg[l][16] + 1;
        end
        if (bad_reg) begin
            stat_cnt_reg[l][17] <= stat_cnt_reg[l][17] + 1;
        end

        if (rst) begin
            valid_reg <= 1'b0;
            drop_reg <= 1'b0;
            bad_reg <= 1'b0;
            for (int n = 0; n < STAT_N; n++) begin
                stat_cnt_reg[l][n] <= '0;
            end
        end
    end
end

// AXI-Lite
logic s_axil_awready_reg = 1'b0;
logic s_axil_wready_reg = 1'b0;
logic s_axil_bvalid_reg = 1'b0;
logic s_axil_arready_reg = 1'b0;
logic [31:0] s_axil_rdata_reg = '0;
logic s_axil_rvalid_reg = 1'b0;

assign s_axil_wr.awready = s_axil_awready_reg;
assign s_axil_wr.wready = s_axil_wready_reg;
assign s_axil_wr.bresp = '0;
assign s_axil_wr.buser = '0;
assign s_axil_wr.bvalid = s_axil_bvalid_reg;

assign s_axil_rd.arready = s_axil_arready_reg;
assign s_axil_rd.rdata = s_axil_rdata_reg;
assign s_axil_rd.rresp = '0;
assign s_axil_rd.ruser = '0;
assign s_axil_rd.rvalid = s_axil_rvalid_reg;

logic s_evt_ready_reg = 1'b0;
assign s_evt_ready = s_evt_ready_reg;

wire [15:0] waddr = {s_axil_wr.awaddr[15:2], 2'b00};
wire [15:0] raddr = {s_axil_rd.araddr[15:2], 2'b00};
wire [31:0] wdata = s_axil_wr.wdata;

// statistics read decode
wire [2:0] stat_rd_lane = raddr[10:8];
wire [4:0] stat_rd_n = raddr[7:3];

// read pipeline: stage 1 selects the statistics word so the wide mux is registered
logic        rd_pend_reg = 1'b0;
logic [15:0] rd_addr_reg = '0;
logic [63:0] stat_rd_reg = '0;

always_ff @(posedge clk) begin
    s_axil_awready_reg <= 1'b0;
    s_axil_wready_reg <= 1'b0;
    s_axil_bvalid_reg <= s_axil_bvalid_reg && !s_axil_wr.bready;

    s_axil_arready_reg <= 1'b0;
    s_axil_rvalid_reg <= s_axil_rvalid_reg && !s_axil_rd.rready;

    s_evt_ready_reg <= 1'b0;
    clear_start_reg <= 1'b0;
    ddr_clear_start_reg <= 1'b0;

    ddr_lookups_reg <= ddr_lookups_reg + 32'(stat_ddr_lookup);
    ddr_hits_reg <= ddr_hits_reg + 32'(stat_ddr_hit);
    ddr_skips_reg <= ddr_skips_reg + 32'(stat_ddr_skip);

    if (host_ddr_valid && host_ddr_ready) begin
        ddr_pend_reg <= 1'b0;
        ddr_wait_reg <= 1'b1;
    end
    if (ddr_wait_reg && host_ddr_done) begin
        ddr_wait_reg <= 1'b0;
        if (ddr_op_reg == 2'd3) begin
            for (int k = 0; k < 7; k++) begin
                ent_data_reg[k] <= 32'(ENTRY_W'(host_ddr_rdata) >> (32*k));
            end
        end
    end
    if (act_valid && act_ready) begin
        act_pend_reg <= 1'b0;
        act_wait_reg <= 1'b1;
    end
    if (act_wait_reg && act_rvalid) begin
        act_wait_reg <= 1'b0;
        act_data_reg[0] <= act_rdata[31:0];
        act_data_reg[1] <= act_rdata[63:32];
    end

    // initial table clear after reset
    if (init_clear_reg) begin
        init_clear_reg <= 1'b0;
        clear_start_reg <= 1'b1;
    end

    // tick
    if (tick_cnt_reg >= tick_div_reg - 1) begin
        tick_cnt_reg <= '0;
        tick_reg <= tick_reg + 1;
    end else begin
        tick_cnt_reg <= tick_cnt_reg + 1;
    end

    if (stat_evt_drop) begin
        evt_drop_reg <= evt_drop_reg + 1;
    end

    // command engine handshakes
    if (host_ent_valid && host_ent_ready) begin
        ent_pend_reg <= 1'b0;
        ent_wait_reg <= !ent_we_reg;
    end
    if (ent_wait_reg && host_ent_rvalid) begin
        ent_wait_reg <= 1'b0;
        for (int k = 0; k < 7; k++) begin
            ent_data_reg[k] <= 32'(ENTRY_W'(host_ent_rdata) >> (32*k));
        end
    end
    if (host_st_valid && host_st_ready) begin
        st_pend_reg <= 1'b0;
        st_wait_reg <= !st_we_reg;
    end
    if (st_wait_reg && host_st_rvalid) begin
        st_wait_reg <= 1'b0;
        for (int k = 0; k < 5; k++) begin
            st_data_reg[k] <= 32'(STATE_W'(host_st_rdata) >> (32*k));
        end
    end
    if (host_nh_valid && host_nh_ready) begin
        nh_pend_reg <= 1'b0;
        nh_wait_reg <= !nh_we_reg;
    end
    if (nh_wait_reg && host_nh_rvalid) begin
        nh_wait_reg <= 1'b0;
        for (int k = 0; k < 4; k++) begin
            nh_data_reg[k] <= 32'(NH_W'(host_nh_rdata) >> (32*k));
        end
    end

    // writes
    if (s_axil_wr.awvalid && s_axil_wr.wvalid && !s_axil_bvalid_reg && !busy) begin
        s_axil_awready_reg <= 1'b1;
        s_axil_wready_reg <= 1'b1;
        s_axil_bvalid_reg <= 1'b1;

        if (waddr >= 16'h0100 && waddr < 16'h011C) begin
            ent_data_reg[3'(((waddr - 16'h0100) >> 2))] <= wdata;
        end
        if (waddr >= 16'h0120 && waddr < 16'h0134) begin
            st_data_reg[3'(((waddr - 16'h0120) >> 2))] <= wdata;
        end
        if (waddr >= 16'h0210 && waddr < 16'h0220) begin
            nh_data_reg[2'(((waddr - 16'h0210) >> 2))] <= wdata;
        end

        case (waddr)
            16'h000C: scratch_reg <= wdata;
            16'h0010: begin
                enable_reg <= wdata[0];
                punt_hdr_reg <= wdata[1];
                bypass_reg <= wdata[15:8];
                egress_en_reg <= wdata[23:16];
            end
            16'h0014: clear_start_reg <= wdata[0];
            16'h0020: seed0_reg <= wdata;
            16'h0024: seed1_reg <= wdata;
            16'h0028: tick_div_reg <= wdata;
            16'h0030: thresh_tcp_reg <= wdata;
            16'h0034: thresh_udp_reg <= wdata;
            16'h0038: begin
                scan_en_reg <= wdata[31];
                scan_interval_reg <= wdata[15:0];
            end
            16'h003C: bubble_reg <= wdata[15:0];
            16'h0064: begin
                ddr_en_reg <= wdata[0];
                ddr_clear_start_reg <= wdata[1] && DDR_ENABLE != 0;
            end
            16'h0140: index_reg <= wdata;
            16'h0144: begin
                entry_t e;
                state_t s;
                e = entry_t'({ent_data_reg[6], ent_data_reg[5], ent_data_reg[4], ent_data_reg[3],
                    ent_data_reg[2], ent_data_reg[1], ent_data_reg[0]});
                s = state_t'({st_data_reg[4], st_data_reg[3], st_data_reg[2], st_data_reg[1], st_data_reg[0]});
                cmd_idx_reg <= IDX_W'(index_reg);
                case (cmd_t'(wdata[3:0]))
                    CMD_WR_ENT: begin
                        ent_pend_reg <= 1'b1;
                        ent_we_reg <= 1'b1;
                        ent_wdata_reg <= e;
                        st_pend_reg <= 1'b1;
                        st_we_reg <= 1'b1;
                        st_wdata_reg <= '0;
                        st_wdata_reg.valid <= e.valid;
                        st_wdata_reg.tcp <= e.key.tcp;
                        st_wdata_reg.ts <= tick_reg;
                    end
                    CMD_WR_ST: begin
                        st_pend_reg <= 1'b1;
                        st_we_reg <= 1'b1;
                        st_wdata_reg <= s;
                    end
                    CMD_RD_ENT: begin
                        ent_pend_reg <= 1'b1;
                        ent_we_reg <= 1'b0;
                    end
                    CMD_RD_ST: begin
                        st_pend_reg <= 1'b1;
                        st_we_reg <= 1'b0;
                    end
                    CMD_CLR: begin
                        ent_pend_reg <= 1'b1;
                        ent_we_reg <= 1'b1;
                        ent_wdata_reg <= '0;
                        st_pend_reg <= 1'b1;
                        st_we_reg <= 1'b1;
                        st_wdata_reg <= '0;
                    end
                    // DDR tier: ignored without one (no request, no wait)
                    CMD_DDR_WR, CMD_DDR_CLR, CMD_DDR_RD: begin
                        if (DDR_ENABLE != 0) begin
                            ddr_pend_reg <= 1'b1;
                            ddr_op_reg <= wdata[3:0] == 4'd6 ? 2'd1 : wdata[3:0] == 4'd7 ? 2'd2 : 2'd3;
                            ddr_wdata_reg <= e;
                        end
                    end
                    CMD_ACT_RC: begin
                        if (DDR_ENABLE != 0) begin
                            act_pend_reg <= 1'b1;
                        end
                    end
                    default: begin end
                endcase
            end
            16'h0200: nh_index_reg <= wdata[9:0];
            16'h0204: begin
                if (wdata[1:0] == 2'd1) begin
                    nh_pend_reg <= 1'b1;
                    nh_we_reg <= 1'b1;
                    nh_wdata_reg <= nh_t'({nh_data_reg[3], nh_data_reg[2], nh_data_reg[1], nh_data_reg[0]});
                end else if (wdata[1:0] == 2'd2) begin
                    nh_pend_reg <= 1'b1;
                    nh_we_reg <= 1'b0;
                end
            end
            default: begin end
        endcase
    end

    // reads, stage 1: accept and capture address plus the statistics word
    if (s_axil_rd.arvalid && !s_axil_rvalid_reg && !rd_pend_reg && !s_axil_arready_reg && !busy) begin
        s_axil_arready_reg <= 1'b1;
        rd_pend_reg <= 1'b1;
        rd_addr_reg <= raddr;
        stat_rd_reg <= stat_rd_n < STAT_N ? stat_cnt_reg[stat_rd_lane][stat_rd_n] : '0;
    end

    // reads, stage 2: respond
    if (rd_pend_reg) begin
        rd_pend_reg <= 1'b0;
        s_axil_rvalid_reg <= 1'b1;
        s_axil_rdata_reg <= '0;

        if (rd_addr_reg >= 16'h1000 && rd_addr_reg < 16'h1800) begin
            if (rd_addr_reg[2]) begin
                s_axil_rdata_reg <= stat_hi_shadow_reg;
            end else begin
                s_axil_rdata_reg <= stat_rd_reg[31:0];
                stat_hi_shadow_reg <= stat_rd_reg[63:32];
            end
        end
        if (rd_addr_reg >= 16'h0100 && rd_addr_reg < 16'h011C) begin
            s_axil_rdata_reg <= ent_data_reg[3'(((rd_addr_reg - 16'h0100) >> 2))];
        end
        if (rd_addr_reg >= 16'h0120 && rd_addr_reg < 16'h0134) begin
            s_axil_rdata_reg <= st_data_reg[3'(((rd_addr_reg - 16'h0120) >> 2))];
        end
        if (rd_addr_reg >= 16'h0210 && rd_addr_reg < 16'h0220) begin
            s_axil_rdata_reg <= nh_data_reg[2'(((rd_addr_reg - 16'h0210) >> 2))];
        end

        case (rd_addr_reg)
            16'h0000: s_axil_rdata_reg <= 32'h4E415447;
            16'h0004: s_axil_rdata_reg <= 32'h00010100;
            16'h0008: s_axil_rdata_reg <= {8'd0, 8'd16, 8'(LANES), 8'(IDX_W)};
            16'h000C: s_axil_rdata_reg <= scratch_reg;
            16'h0010: s_axil_rdata_reg <= {8'd0, egress_en_reg, bypass_reg, 6'd0, punt_hdr_reg, enable_reg};
            16'h0014: s_axil_rdata_reg <= {31'd0, clear_busy || clear_start_reg};
            16'h0020: s_axil_rdata_reg <= seed0_reg;
            16'h0024: s_axil_rdata_reg <= seed1_reg;
            16'h0028: s_axil_rdata_reg <= tick_div_reg;
            16'h002C: s_axil_rdata_reg <= tick_reg;
            16'h0030: s_axil_rdata_reg <= thresh_tcp_reg;
            16'h0034: s_axil_rdata_reg <= thresh_udp_reg;
            16'h0038: s_axil_rdata_reg <= {scan_en_reg, 15'd0, scan_interval_reg};
            16'h003C: s_axil_rdata_reg <= {16'd0, bubble_reg};
            16'h0060: begin
                if (DDR_ENABLE != 0) begin
                    s_axil_rdata_reg <= {8'd0, 8'(DDR_MAX_OUT), 8'(DDR_BUCKET_W), 3'd0, ddr_active,
                        ddr_clear_busy || ddr_clear_start_reg, ddr_en_reg, ddr_calib, 1'b1};
                end
            end
            16'h0064: s_axil_rdata_reg <= {31'd0, ddr_en_reg};
            16'h0068: s_axil_rdata_reg <= ddr_lookups_reg;
            16'h006C: s_axil_rdata_reg <= ddr_hits_reg;
            16'h0070: s_axil_rdata_reg <= ddr_skips_reg;
            16'h0148: s_axil_rdata_reg <= act_data_reg[0];
            16'h014C: s_axil_rdata_reg <= act_data_reg[1];
            16'h0140: s_axil_rdata_reg <= index_reg;
            16'h0144: s_axil_rdata_reg <= {31'd0, busy};
            16'h0200: s_axil_rdata_reg <= {22'd0, nh_index_reg};
            16'h0204: s_axil_rdata_reg <= {31'd0, busy};
            16'h0300: s_axil_rdata_reg <= {31'd0, s_evt_valid};
            16'h0304: s_axil_rdata_reg <= s_evt_valid ? s_evt[31:0] : '0;
            16'h0308: begin
                s_axil_rdata_reg <= s_evt_valid ? s_evt[63:32] : '0;
                s_evt_ready_reg <= s_evt_valid;
            end
            16'h0310: s_axil_rdata_reg <= evt_drop_reg;
            default: begin end
        endcase
    end

    if (rst) begin
        s_axil_awready_reg <= 1'b0;
        s_axil_wready_reg <= 1'b0;
        s_axil_bvalid_reg <= 1'b0;
        s_axil_arready_reg <= 1'b0;
        s_axil_rvalid_reg <= 1'b0;
        s_evt_ready_reg <= 1'b0;
        rd_pend_reg <= 1'b0;

        enable_reg <= 1'b0;
        punt_hdr_reg <= 1'b0;
        bypass_reg <= 8'hff;
        egress_en_reg <= 8'hff;
        scratch_reg <= '0;
        seed0_reg <= '1;
        seed1_reg <= '1;
        tick_div_reg <= TICK_DIV_RST;
        tick_cnt_reg <= '0;
        tick_reg <= '0;
        thresh_tcp_reg <= 32'd7440000;
        thresh_udp_reg <= 32'd300000;
        scan_en_reg <= 1'b1;
        scan_interval_reg <= 16'd5;
        bubble_reg <= 16'd16;
        clear_start_reg <= 1'b0;
        init_clear_reg <= 1'b1;

        ent_pend_reg <= 1'b0;
        ent_wait_reg <= 1'b0;
        st_pend_reg <= 1'b0;
        st_wait_reg <= 1'b0;
        nh_pend_reg <= 1'b0;
        nh_wait_reg <= 1'b0;
        evt_drop_reg <= '0;
        ddr_en_reg <= 1'b0;
        ddr_clear_start_reg <= 1'b0;
        ddr_pend_reg <= 1'b0;
        ddr_wait_reg <= 1'b0;
        act_pend_reg <= 1'b0;
        act_wait_reg <= 1'b0;
        ddr_lookups_reg <= '0;
        ddr_hits_reg <= '0;
        ddr_skips_reg <= '0;
    end
end

endmodule

`resetall
