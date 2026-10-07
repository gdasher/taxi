# NAT gateway shim (natgw)

A NAT fast path that sits between the 25G MAC lanes and the cndm NIC core.
Design plan: "NAT Gateway FPGA Design Plan" (reviewed and approved 2026-10-07).

All shim logic runs on one clock, `clk` = `pcie_clk` (250 MHz), with 128-bit
AXI-Stream per lane. Byte 0 of a frame is `tdata[7:0]` of its first beat.
Frames carry no FCS. `natgw_pkg.sv` holds every shared type; `tb/natgw_model.py`
mirrors it bit for bit and is the scoreboard for all tests.

## Block interfaces

All valid/ready pairs follow AXI-Stream rules: a transfer happens on a cycle
where both are high; `valid` must not depend on `ready`.

### natgw_parser (one per lane)

```
parameter LANE = 0, USER_W = 49
input  clk, rst
taxi_axis_if.snk s_axis          // from ingress FIFO: DATA_W 128, KEEP_W 16, LAST, USER_W (tuser passed through untouched)
taxi_axis_if.src m_axis_hold     // every beat of every frame, unmodified, same tuser
output m_desc_valid; input m_desc_ready
output key_t  m_desc_key
output logic  m_desc_lookup      // 1 = look key up (reason MISS or FINRST)
output logic  m_desc_fin, m_desc_rst
output logic [15:0] m_desc_len   // frame length in bytes
output meta_t m_desc_meta
input  cfg_bypass                // sampled per frame at EOP
```

Exactly one descriptor per frame, emitted after the frame's last beat has been
accepted. Only the first 64 bytes are inspected. Classification and precedence
are defined by `natgw_model.parse()`; key and meta fields are extracted from
the fixed offsets whatever the reason. `meta.reason` is the pre-lookup reason
(RSN_MISS when the frame is a lookup candidate with no exception, RSN_FINRST
for FIN/RST). The parser stalls `s_axis` rather than drop.

### natgw_lookup (shared)

```
parameter BUCKET_W = 16, RAM_PIPE = 3, IDX_W = BUCKET_W+3
input  clk, rst
// per-lane key inputs (round-robin, at most one accepted per cycle)
input  s_key_valid[LANES]; output s_key_ready[LANES]
input  key_t s_key[LANES]; input s_key_lookup[LANES], s_key_fin[LANES], s_key_rst[LANES]
input  [15:0] s_key_len[LANES]
// results: fixed latency, no backpressure, in order
output m_res_valid; output [2:0] m_res_lane; output result_t m_res
// hit stream to natgw_state (same cycle as the matching m_res)
output m_hit_valid; output [IDX_W-1:0] m_hit_idx; output [15:0] m_hit_len; output m_hit_fin, m_hit_rst
input  bubble_req                 // state block wants a free slot under load
input  [15:0] cfg_bubble_period   // insert one idle cycle every N cycles while bubble_req (0 = never)
input  [31:0] cfg_seed0, cfg_seed1
// host entry access (port B), one op per cycle when ready
input  host_ent_valid; output host_ent_ready; input host_ent_we
input  [IDX_W-1:0] host_ent_idx; input entry_t host_ent_wdata
output host_ent_rvalid; output entry_t host_ent_rdata
// host next-hop access
input  host_nh_valid; output host_nh_ready; input host_nh_we
input  [9:0] host_nh_idx; input nh_t host_nh_wdata
output host_nh_rvalid; output nh_t host_nh_rdata
// clear: zero every slot of both tables
input  clear_start; output clear_busy
```

Lookup = CRC-32C(seed0) and CRC-32(seed1) of the key; bucket = low BUCKET_W
bits; slots T0[0..3] then T1[0..3]; first valid match wins.
idx = {table, bucket, slot}. Frames with `lookup = 0` still pass through the
pipeline (keeping per-lane order) and return `hit = 0, hash = 0`.
`result.hash` = h0 for lookup frames. On a hit, `result` carries the entry's
action and the next-hop entry `nh[nh_idx]`.

### natgw_state (shared)

```
parameter IDX_W = 19, RAM_PIPE = 3, EVT_DEPTH = 1024
input  clk, rst
input  s_hit_valid; input [IDX_W-1:0] s_hit_idx; input [15:0] s_hit_len; input s_hit_fin, s_hit_rst
output bubble_req
input  host_st_valid; output host_st_ready; input host_st_we
input  [IDX_W-1:0] host_st_idx; input state_t host_st_wdata
output host_st_rvalid; output state_t host_st_rdata
output m_evt_valid; input m_evt_ready; output [63:0] m_evt   // {type[3:0], 4'd0, idx[23:0]} in [63:32], tick in [31:0]
input  [31:0] cfg_tick, cfg_thresh_tcp, cfg_thresh_udp
input  cfg_scan_en; input [15:0] cfg_scan_interval
input  clear_start; output clear_busy
output stat_evt_drop              // pulse: a FIN/RST event was lost to a full FIFO
```

Hit: ts = tick, pkts += 1, bytes += len, evp = 0, fin |= hit_fin,
rst |= hit_rst; a FIN or RST hit also queues an event (FIN wins if both).
Scanner: for each idx in turn, every `cfg_scan_interval` cycles when a slot is
free: if valid and !evp and (tick - ts) > thresh[tcp], queue EVT_IDLE and set
evp. The scanner pauses while the event FIFO has fewer than 4 free entries.
After any lost event an EVT_OVF is queued once space frees.
Read-modify-write hazards between back-to-back operations on one index are
resolved by forwarding.

### natgw_rewrite (one per lane)

```
parameter LANE = 0, USER_W = 49
input  clk, rst
taxi_axis_if.snk s_axis_hold
input  s_res_valid;  output s_res_ready;  input result_t s_res
input  s_meta_valid; output s_meta_ready; input meta_t   s_meta
taxi_axis_if.src m_axis_fwd      // DATA_W 128, DEST_W 3 (egress lane), USER_W 1 (always 0)
taxi_axis_if.src m_axis_punt     // DATA_W 128, USER_W = USER_W (tuser of the frame, on every beat)
input  cfg_punt_hdr, input [7:0] cfg_egress_en
output stat_valid; output [7:0] stat_reason   // one pulse per frame (RSN_FWD when forwarded)
```

At each frame's first beat, pops one result and one meta, decides as
`ShimModel.process()`, and streams the frame to `m_axis_fwd` (rewritten) or
`m_axis_punt` (unchanged, after a 16-byte punt header when `cfg_punt_hdr` and
the reason is not BYPASS). The header beat carries the frame's first-beat tuser.
