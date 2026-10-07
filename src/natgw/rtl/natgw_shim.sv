// SPDX-License-Identifier: CERN-OHL-S-2.0
/*

NAT gateway shim: top level

Sits between eight MAC lanes and the cndm core's MAC-side ports. All shim
logic runs on clk (pcie_clk, 250 MHz) at 128 bits per lane. Frames cross in
from each lane's RX clock, are parsed and looked up, and are either forwarded
(rewritten) to their egress lane or punted to the core on their ingress port.

*/

`resetall
`timescale 1ns / 1ps
`default_nettype none

module natgw_shim
    import natgw_pkg::*;
#(
    parameter BUCKET_W = 16,
    parameter RAM_PIPE = 3,
    parameter TICK_DIV_RST = 250000,
    parameter HOLD_DEPTH = 16384,
    parameter DESC_DEPTH = 32,
    parameter CDC_DEPTH = 16384
)
(
    input  wire logic  clk,
    input  wire logic  rst,

    /*
     * Control registers
     */
    taxi_axil_if.wr_slv  s_axil_wr,
    taxi_axil_if.rd_slv  s_axil_rd,

    /*
     * MAC side
     */
    input  wire logic    mac_tx_clk[LANES],
    input  wire logic    mac_tx_rst[LANES],
    taxi_axis_if.src     m_axis_mac_tx[LANES],
    taxi_axis_if.snk     s_axis_mac_tx_cpl[LANES],

    input  wire logic    mac_rx_clk[LANES],
    input  wire logic    mac_rx_rst[LANES],
    taxi_axis_if.snk     s_axis_mac_rx[LANES],

    /*
     * Core side (same clocks as the MAC side)
     */
    taxi_axis_if.snk     s_axis_core_tx[LANES],
    taxi_axis_if.src     m_axis_core_tx_cpl[LANES],
    taxi_axis_if.src     m_axis_core_rx[LANES],

    /*
     * Status
     */
    output wire logic    clear_busy
);

localparam IDX_W = BUCKET_W + 3;
localparam RX_USER_W = s_axis_mac_rx[0].USER_W;
localparam DATA_W = 128;
localparam KEEP_W = DATA_W/8;

// configuration
wire              cfg_enable;
wire              cfg_punt_hdr;
wire [LANES-1:0]  cfg_bypass;
wire [LANES-1:0]  cfg_egress_en;
wire [31:0]       cfg_seed0, cfg_seed1;
wire [31:0]       cfg_tick;
wire [31:0]       cfg_thresh_tcp, cfg_thresh_udp;
wire              cfg_scan_en;
wire [15:0]       cfg_scan_interval;
wire [15:0]       cfg_bubble_period;

wire clear_start;
wire lookup_clear_busy;
wire state_clear_busy;
assign clear_busy = lookup_clear_busy || state_clear_busy;

// per-lane key inputs to the lookup engine
wire         key_valid[LANES];
wire         key_ready[LANES];
key_t        key_data[LANES];
wire         key_lookup[LANES];
wire         key_fin[LANES];
wire         key_rst[LANES];
wire [15:0]  key_len[LANES];

// results
wire              res_valid;
wire [LANE_W-1:0] res_lane;
result_t          res_data;

// hits
wire              hit_valid;
wire [IDX_W-1:0]  hit_idx;
wire [15:0]       hit_len;
wire              hit_fin, hit_rst;
wire              bubble_req;

// host access
wire              host_ent_valid, host_ent_ready, host_ent_we, host_ent_rvalid;
wire [IDX_W-1:0]  host_ent_idx;
entry_t           host_ent_wdata, host_ent_rdata;
wire              host_st_valid, host_st_ready, host_st_we, host_st_rvalid;
wire [IDX_W-1:0]  host_st_idx;
state_t           host_st_wdata, host_st_rdata;
wire              host_nh_valid, host_nh_ready, host_nh_we, host_nh_rvalid;
wire [9:0]        host_nh_idx;
nh_t              host_nh_wdata, host_nh_rdata;

wire        evt_valid, evt_ready;
wire [63:0] evt_data;
wire        stat_evt_drop;

wire        stat_valid[LANES];
wire [7:0]  stat_reason[LANES];
wire        stat_ingress_drop[LANES];
wire        stat_ingress_bad[LANES];

// forwarded frames into the lane switch
taxi_axis_if #(.DATA_W(DATA_W), .DEST_EN(1), .DEST_W(LANE_W), .USER_EN(1), .USER_W(1)) fwd_axis[LANES]();
taxi_axis_if #(.DATA_W(DATA_W), .DEST_EN(1), .DEST_W(1), .USER_EN(1), .USER_W(1)) egress_axis[LANES]();

for (genvar n = 0; n < LANES; n = n + 1) begin : lane

    // ingress: MAC RX clock, 64 bits -> clk, 128 bits; drops bad and overflowing frames
    taxi_axis_if #(.DATA_W(DATA_W), .USER_EN(1), .USER_W(RX_USER_W)) rx_axis();
    taxi_axis_if #(.DATA_W(DATA_W), .USER_EN(1), .USER_W(RX_USER_W)) hold_in_axis();
    taxi_axis_if #(.DATA_W(DATA_W), .USER_EN(1), .USER_W(RX_USER_W)) hold_out_axis();
    taxi_axis_if #(.DATA_W(DATA_W), .USER_EN(1), .USER_W(RX_USER_W)) punt_axis();

    taxi_axis_async_fifo_adapter #(
        .DEPTH(CDC_DEPTH),
        .RAM_PIPELINE(2),
        .FRAME_FIFO(1),
        .USER_BAD_FRAME_VALUE(1'b1),
        .USER_BAD_FRAME_MASK(1'b1),
        .DROP_OVERSIZE_FRAME(1),
        .DROP_BAD_FRAME(1),
        .DROP_WHEN_FULL(1)
    )
    ingress_fifo_inst (
        .s_clk(mac_rx_clk[n]),
        .s_rst(mac_rx_rst[n]),
        .s_axis(s_axis_mac_rx[n]),
        .m_clk(clk),
        .m_rst(rst),
        .m_axis(rx_axis),
        .s_pause_req(1'b0),
        .s_pause_ack(),
        .m_pause_req(1'b0),
        .m_pause_ack(),
        .s_status_depth(),
        .s_status_depth_commit(),
        .s_status_overflow(),
        .s_status_bad_frame(),
        .s_status_good_frame(),
        .m_status_depth(),
        .m_status_depth_commit(),
        .m_status_overflow(stat_ingress_drop[n]),
        .m_status_bad_frame(stat_ingress_bad[n]),
        .m_status_good_frame()
    );

    // parser
    wire        desc_valid;
    wire        desc_ready;
    key_t       desc_key;
    wire        desc_lookup, desc_fin, desc_rst;
    wire [15:0] desc_len;
    meta_t      desc_meta;

    natgw_parser #(
        .LANE(n),
        .USER_W(RX_USER_W)
    )
    parser_inst (
        .clk(clk),
        .rst(rst),
        .s_axis(rx_axis),
        .m_axis_hold(hold_in_axis),
        .m_desc_valid(desc_valid),
        .m_desc_ready(desc_ready),
        .m_desc_key(desc_key),
        .m_desc_lookup(desc_lookup),
        .m_desc_fin(desc_fin),
        .m_desc_rst(desc_rst),
        .m_desc_len(desc_len),
        .m_desc_meta(desc_meta),
        .cfg_bypass(cfg_bypass[n])
    );

    // hold FIFO: frames wait here for their lookup result
    taxi_axis_fifo #(
        .DEPTH(HOLD_DEPTH),
        .RAM_PIPELINE(1),
        .FRAME_FIFO(0)
    )
    hold_fifo_inst (
        .clk(clk),
        .rst(rst),
        .s_axis(hold_in_axis),
        .m_axis(hold_out_axis),
        .pause_req(1'b0),
        .pause_ack(),
        .status_depth(),
        .status_depth_commit(),
        .status_overflow(),
        .status_bad_frame(),
        .status_good_frame()
    );

    // descriptor split: key to the lookup engine, meta to the rewrite stage
    localparam KEYQ_W = KEY_W + 3 + 16;

    wire keyq_ready, metaq_ready;
    wire [KEYQ_W-1:0] keyq_out;
    wire meta_valid, meta_ready;
    meta_t meta_out;

    assign desc_ready = keyq_ready && metaq_ready;

    natgw_fifo #(
        .DATA_W(KEYQ_W),
        .DEPTH(4)
    )
    key_fifo_inst (
        .clk(clk),
        .rst(rst),
        .s_valid(desc_valid && metaq_ready),
        .s_ready(keyq_ready),
        .s_data({desc_len, desc_rst, desc_fin, desc_lookup, desc_key}),
        .m_valid(key_valid[n]),
        .m_ready(key_ready[n]),
        .m_data(keyq_out),
        .overflow()
    );

    assign key_data[n] = keyq_out[KEY_W-1:0];
    assign key_lookup[n] = keyq_out[KEY_W];
    assign key_fin[n] = keyq_out[KEY_W+1];
    assign key_rst[n] = keyq_out[KEY_W+2];
    assign key_len[n] = keyq_out[KEY_W+3 +: 16];

    natgw_fifo #(
        .DATA_W(META_W),
        .DEPTH(DESC_DEPTH)
    )
    meta_fifo_inst (
        .clk(clk),
        .rst(rst),
        .s_valid(desc_valid && keyq_ready),
        .s_ready(metaq_ready),
        .s_data(desc_meta),
        .m_valid(meta_valid),
        .m_ready(meta_ready),
        .m_data(meta_out),
        .overflow()
    );

    // results for this lane; never more in flight than meta FIFO entries
    wire lres_valid, lres_ready;
    result_t lres_data;
    wire lres_overflow;

    natgw_fifo #(
        .DATA_W(RESULT_W),
        .DEPTH(DESC_DEPTH + 8)
    )
    res_fifo_inst (
        .clk(clk),
        .rst(rst),
        .s_valid(res_valid && res_lane == LANE_W'(n)),
        .s_ready(),
        .s_data(res_data),
        .m_valid(lres_valid),
        .m_ready(lres_ready),
        .m_data(lres_data),
        .overflow(lres_overflow)
    );

    // synthesis translate_off
    always_ff @(posedge clk) begin
        if (!rst && lres_overflow) begin
            $error("natgw_shim: result FIFO overflow on lane %0d", n);
        end
    end
    // synthesis translate_on

    natgw_rewrite #(
        .LANE(n),
        .USER_W(RX_USER_W)
    )
    rewrite_inst (
        .clk(clk),
        .rst(rst),
        .s_axis_hold(hold_out_axis),
        .s_res_valid(lres_valid),
        .s_res_ready(lres_ready),
        .s_res(lres_data),
        .s_meta_valid(meta_valid),
        .s_meta_ready(meta_ready),
        .s_meta(meta_out),
        .m_axis_fwd(fwd_axis[n]),
        .m_axis_punt(punt_axis),
        .cfg_punt_hdr(cfg_punt_hdr),
        .cfg_egress_en(cfg_egress_en),
        .stat_valid(stat_valid[n]),
        .stat_reason(stat_reason[n])
    );

    // punt path: clk, 128 bits -> MAC RX clock, 64 bits, into the core
    taxi_axis_async_fifo_adapter #(
        .DEPTH(CDC_DEPTH),
        .RAM_PIPELINE(2),
        .FRAME_FIFO(1),
        .USER_BAD_FRAME_VALUE(1'b1),
        .USER_BAD_FRAME_MASK(1'b1),
        .DROP_OVERSIZE_FRAME(1),
        .DROP_BAD_FRAME(0),
        .DROP_WHEN_FULL(0)
    )
    punt_fifo_inst (
        .s_clk(clk),
        .s_rst(rst),
        .s_axis(punt_axis),
        .m_clk(mac_rx_clk[n]),
        .m_rst(mac_rx_rst[n]),
        .m_axis(m_axis_core_rx[n]),
        .s_pause_req(1'b0),
        .s_pause_ack(),
        .m_pause_req(1'b0),
        .m_pause_ack(),
        .s_status_depth(),
        .s_status_depth_commit(),
        .s_status_overflow(),
        .s_status_bad_frame(),
        .s_status_good_frame(),
        .m_status_depth(),
        .m_status_depth_commit(),
        .m_status_overflow(),
        .m_status_bad_frame(),
        .m_status_good_frame()
    );

    // egress: clk, 128 bits -> MAC TX clock, 64 bits; whole frames only, so the MAC never underflows
    taxi_axis_if #(.DATA_W(m_axis_mac_tx[0].DATA_W), .USER_EN(1), .USER_W(1)) shim_tx_axis();

    taxi_axis_async_fifo_adapter #(
        .DEPTH(CDC_DEPTH),
        .RAM_PIPELINE(2),
        .FRAME_FIFO(1),
        .USER_BAD_FRAME_VALUE(1'b1),
        .USER_BAD_FRAME_MASK(1'b1),
        .DROP_OVERSIZE_FRAME(1),
        .DROP_BAD_FRAME(0),
        .DROP_WHEN_FULL(0)
    )
    egress_fifo_inst (
        .s_clk(clk),
        .s_rst(rst),
        .s_axis(egress_axis[n]),
        .m_clk(mac_tx_clk[n]),
        .m_rst(mac_tx_rst[n]),
        .m_axis(shim_tx_axis),
        .s_pause_req(1'b0),
        .s_pause_ack(),
        .m_pause_req(1'b0),
        .m_pause_ack(),
        .s_status_depth(),
        .s_status_depth_commit(),
        .s_status_overflow(),
        .s_status_bad_frame(),
        .s_status_good_frame(),
        .m_status_depth(),
        .m_status_depth_commit(),
        .m_status_overflow(),
        .m_status_bad_frame(),
        .m_status_good_frame()
    );

    natgw_tx_merge tx_merge_inst (
        .clk(mac_tx_clk[n]),
        .rst(mac_tx_rst[n]),
        .s_axis_core(s_axis_core_tx[n]),
        .s_axis_shim(shim_tx_axis),
        .m_axis_mac(m_axis_mac_tx[n]),
        .s_axis_mac_cpl(s_axis_mac_tx_cpl[n]),
        .m_axis_core_cpl(m_axis_core_tx_cpl[n])
    );

end

// lane switch: hits to their egress lane (tdest = egress lane)
taxi_axis_switch #(
    .S_COUNT(LANES),
    .M_COUNT(LANES),
    .AUTO_ADDR(1'b1),
    .UPDATE_TID(1'b0),
    .S_REG_TYPE(0),
    .M_REG_TYPE(2),
    .ARB_ROUND_ROBIN(1'b1),
    .ARB_LSB_HIGH_PRIO(1'b1)
)
switch_inst (
    .clk(clk),
    .rst(rst),
    .s_axis(fwd_axis),
    .m_axis(egress_axis)
);

natgw_lookup #(
    .BUCKET_W(BUCKET_W),
    .RAM_PIPE(RAM_PIPE)
)
lookup_inst (
    .clk(clk),
    .rst(rst),
    .s_key_valid(key_valid),
    .s_key_ready(key_ready),
    .s_key(key_data),
    .s_key_lookup(key_lookup),
    .s_key_fin(key_fin),
    .s_key_rst(key_rst),
    .s_key_len(key_len),
    .m_res_valid(res_valid),
    .m_res_lane(res_lane),
    .m_res(res_data),
    .m_hit_valid(hit_valid),
    .m_hit_idx(hit_idx),
    .m_hit_len(hit_len),
    .m_hit_fin(hit_fin),
    .m_hit_rst(hit_rst),
    .bubble_req(bubble_req),
    .cfg_bubble_period(cfg_bubble_period),
    .cfg_seed0(cfg_seed0),
    .cfg_seed1(cfg_seed1),
    .host_ent_valid(host_ent_valid),
    .host_ent_ready(host_ent_ready),
    .host_ent_we(host_ent_we),
    .host_ent_idx(host_ent_idx),
    .host_ent_wdata(host_ent_wdata),
    .host_ent_rvalid(host_ent_rvalid),
    .host_ent_rdata(host_ent_rdata),
    .host_nh_valid(host_nh_valid),
    .host_nh_ready(host_nh_ready),
    .host_nh_we(host_nh_we),
    .host_nh_idx(host_nh_idx),
    .host_nh_wdata(host_nh_wdata),
    .host_nh_rvalid(host_nh_rvalid),
    .host_nh_rdata(host_nh_rdata),
    .clear_start(clear_start),
    .clear_busy(lookup_clear_busy)
);

natgw_state #(
    .IDX_W(IDX_W),
    .RAM_PIPE(RAM_PIPE)
)
state_inst (
    .clk(clk),
    .rst(rst),
    .s_hit_valid(hit_valid),
    .s_hit_idx(hit_idx),
    .s_hit_len(hit_len),
    .s_hit_fin(hit_fin),
    .s_hit_rst(hit_rst),
    .bubble_req(bubble_req),
    .host_st_valid(host_st_valid),
    .host_st_ready(host_st_ready),
    .host_st_we(host_st_we),
    .host_st_idx(host_st_idx),
    .host_st_wdata(host_st_wdata),
    .host_st_rvalid(host_st_rvalid),
    .host_st_rdata(host_st_rdata),
    .m_evt_valid(evt_valid),
    .m_evt_ready(evt_ready),
    .m_evt(evt_data),
    .cfg_tick(cfg_tick),
    .cfg_thresh_tcp(cfg_thresh_tcp),
    .cfg_thresh_udp(cfg_thresh_udp),
    .cfg_scan_en(cfg_scan_en),
    .cfg_scan_interval(cfg_scan_interval),
    .clear_start(clear_start),
    .clear_busy(state_clear_busy),
    .stat_evt_drop(stat_evt_drop)
);

natgw_regs #(
    .IDX_W(IDX_W),
    .TICK_DIV_RST(TICK_DIV_RST)
)
regs_inst (
    .clk(clk),
    .rst(rst),
    .s_axil_wr(s_axil_wr),
    .s_axil_rd(s_axil_rd),
    .cfg_enable(cfg_enable),
    .cfg_punt_hdr(cfg_punt_hdr),
    .cfg_bypass(cfg_bypass),
    .cfg_egress_en(cfg_egress_en),
    .cfg_seed0(cfg_seed0),
    .cfg_seed1(cfg_seed1),
    .cfg_tick(cfg_tick),
    .cfg_thresh_tcp(cfg_thresh_tcp),
    .cfg_thresh_udp(cfg_thresh_udp),
    .cfg_scan_en(cfg_scan_en),
    .cfg_scan_interval(cfg_scan_interval),
    .cfg_bubble_period(cfg_bubble_period),
    .clear_start(clear_start),
    .clear_busy(clear_busy),
    .host_ent_valid(host_ent_valid),
    .host_ent_ready(host_ent_ready),
    .host_ent_we(host_ent_we),
    .host_ent_idx(host_ent_idx),
    .host_ent_wdata(host_ent_wdata),
    .host_ent_rvalid(host_ent_rvalid),
    .host_ent_rdata(host_ent_rdata),
    .host_st_valid(host_st_valid),
    .host_st_ready(host_st_ready),
    .host_st_we(host_st_we),
    .host_st_idx(host_st_idx),
    .host_st_wdata(host_st_wdata),
    .host_st_rvalid(host_st_rvalid),
    .host_st_rdata(host_st_rdata),
    .host_nh_valid(host_nh_valid),
    .host_nh_ready(host_nh_ready),
    .host_nh_we(host_nh_we),
    .host_nh_idx(host_nh_idx),
    .host_nh_wdata(host_nh_wdata),
    .host_nh_rvalid(host_nh_rvalid),
    .host_nh_rdata(host_nh_rdata),
    .s_evt_valid(evt_valid),
    .s_evt_ready(evt_ready),
    .s_evt(evt_data),
    .stat_valid(stat_valid),
    .stat_reason(stat_reason),
    .stat_ingress_drop(stat_ingress_drop),
    .stat_ingress_bad(stat_ingress_bad),
    .stat_evt_drop(stat_evt_drop)
);

endmodule

`resetall
