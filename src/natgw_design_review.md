# `natgw` End-to-End Architecture & Design Review

## 1. Executive Summary

The `natgw` stack is an exceptionally cohesive, vertically integrated hardware-accelerated NAT44 gateway built on top of the Corundum (`cndm`) FPGA NIC architecture and FD.io VPP. Rather than attempting to implement a full router or stateful TCP/NAT control plane in RTL, the design follows a **strict fast-path / slow-path split**:
- **Software (VPP + `nat44-ed` + `libnatgw`)** owns session creation, TCP state transitions (`SYN`, `FIN`, `RST`), ARP/FIB resolution, cuckoo hash placement (BFS relocation), and two-tier (URAM $\leftrightarrow$ DDR4) flow migration.
- **Hardware (`natgw_shim`)** acts as a deterministic, wire-speed bump-in-the-wire between the 8$\times$ 10G MACs and the `cndm` DMA core, executing 8-way on-chip UltraRAM and 4-way off-chip DDR4 cuckoo lookups, RFC 1624 incremental checksum updates, L2/L3/L4 header rewrites, and background aging/activity tracking.

```mermaid
flowchart TB
    subgraph HW["FPGA Hardware (250 MHz, 128-bit AXI4-Stream per Lane)"]
        MAC_RX["8x 10G GTY MAC RX"] --> ING_FIFO["Ingress Async FIFO (16 KB)"]
        ING_FIFO --> PARSER["natgw_parser\n(64B Header + Classifier)"]
        PARSER -->|Frame Beats| HOLD["Hold FIFO (16 KB)"]
        PARSER -->|112b Key| LOOKUP["natgw_lookup\n(2x4-way URAM, 256k flows)"]
        LOOKUP -->|Hit| STATE["natgw_state\n(URAM Counters + Scanner)"]
        LOOKUP -->|On-Chip Miss| DDR["natgw_ddr + natgw_actmap\n(2x2-way DDR4-2400, 4M flows)"]
        LOOKUP -->|Hit Result| REWRITE["natgw_rewrite\n(L2/L3/L4 Patch + Checksum)"]
        DDR -->|DDR Hit / Miss Result| REWRITE
        HOLD --> REWRITE
        REWRITE -->|Forwarded (tdest=lane)| SWITCH["natgw_switch\n(8x8 Round-Robin Crossbar)"]
        SWITCH --> EGR_FIFO["Egress Async FIFO (16 KB)"]
        EGR_FIFO --> TX_MERGE["natgw_tx_merge"]
        TX_MERGE --> MAC_TX["8x 10G GTY MAC TX"]
        REWRITE -->|Punted (+16B Punt Hdr)| PUNT_FIFO["Punt Async FIFO (16 KB)"]
    end

    subgraph SW["Guest VM Software Stack (Debian 13 / VPP 26.06 / DPDK 26.03)"]
        PUNT_FIFO -->|PCIe Gen3 x16 DMA| PMD["net_cndm PMD + natgw_flow\n(rte_flow + Tier Policy + Cuckoo BFS)"]
        PMD -->|Core TX| TX_MERGE
        PMD <-->|BAR0 +0x800000 MMIO| LOOKUP
        PMD <-->|BAR0 +0x800000 MMIO| DDR
        PMD <-->|BAR0 +0x800000 MMIO| STATE
        PMD <-->|Slow-Path Packets & rte_flow| VPP["VPP nat44-ed + natgw_offload"]
        VPP <-->|vpp_papi Socket| WANMGR["wanmgr\n(Multi-WAN Health, DHCP & ECMP)"]
        VPP -->|Stats Segment| MON["natgw_monitor\n(DuckDB + SQLite + HTTPS UI)"]
        WANMGR -->|status.json| MON
    end
```

### Overall Assessment by Layer

| Layer | Component | Maturity | Key Strengths | Primary Risks / Improvement Areas |
| :--- | :--- | :--- | :--- | :--- |
| **1. FPGA RTL** | [`natgw/rtl/`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_shim.sv), [`fpga_core_nat.sv`](file:///home/parallels/git/taxi/src/cndm/board/Alveo/fpga/rtl/fpga_core_nat.sv) | High | Deadlock-free DDR bypass under saturation; zero-bubble rewrite pipeline; FLR auto-clear state machine. | Egress congestion can HoL-block ingress lanes (`DROP_WHEN_FULL=0` on egress FIFO); cannot push/pop 802.1Q tags; tight U200 timing (`+0.011 ns` WNS). |
| **2. Verification** | [`natgw/tb/`](file:///home/parallels/git/taxi/src/natgw/VERIFICATION.md), [`natgw_model.c`](file:///home/parallels/git/taxi/src/natgw/sw/model/natgw_model.c) | Very High | Three-way golden equivalence (Python, C, SV RTL); 100% functional coverage across 7 suites. | Lack of automated post-synthesis / gate-level CDC & timing sim for the async DDR4 clock crossing. |
| **3. HAL (`libnatgw`)** | [`natgw.h`](file:///home/parallels/git/taxi/src/natgw/sw/include/natgw.h), [`natgw.c`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c) | Very High | Make-before-break cuckoo relocation & tier migration (`natgw_tier_promote`/`demote`); zero-dependency C. | Bit-by-bit serial packing (`put_bits`/`get_bits`) and software CRC loops in [`natgw.c`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L16-L157) add CPU overhead on high-churn flow setups. |
| **4. DPDK / `rte_flow`** | [`natgw_flow.c`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c), [`cndm_natgw.c`](file:///home/parallels/git/taxi/src/cndm/dpdk/cndm/cndm_natgw.c) | High | Clean `rte_flow` mapping; hysteresis-based balanced tier policy; safe activity-bit pre-clearing on slot reuse. | `natgw_flow_poll()` polls hardware state over MMIO on *every* cycle for `idle_pending` flows before their timeout expires. |
| **5. VPP Offload** | [`natgw_offload.c`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c), [Patches](file:///home/parallels/git/taxi/src/natgw/sw/vpp/patches/0001-nat44-ed-session-event-hooks-for-hardware-offload.patch) | High | Queries MMIO counters outside VPP worker barrier; automatic FIB/ARP next-hop revalidation. | Full session pool walk to find active ports in [`ngo_sync()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L760-L762); unconditional $O(N)$ `vec_search` on `pending` in [`ngo_session_free()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L536). |
| **6. Control & Monitor** | [`wanmgr`](file:///home/parallels/git/taxi/src/natgw/sw/wanmgr/wanmgr/manager.py), [`natgw_monitor`](file:///home/parallels/git/taxi/src/natgw/sw/monitor/natgw_monitor/collector.py) | High | Stateless VPP reconciliation; hybrid DuckDB + SQLite storage with integer scaling; rich outage snapshots. | `/32` probe routes in FIB table 0 steer LAN client traffic to dead WANs if public DNS IPs are used as probe targets; blocking `ping` holds VPP API lock per target. |
| **7. Deployment** | [`natgw/sw/deploy/`](file:///home/parallels/git/taxi/src/natgw/sw/deploy/README.md) | High | Idempotent host/guest setup scripts; automated Proxmox hookscript FPGA programming & FLR handling. | Single-VM SPOF (no VRRP/HA state sync); requires symmetric VLAN tagging across LAN and WAN interfaces. |

---

## 2. Layer-by-Layer Technical Evaluation

### Layer 1: FPGA RTL & Hardware Pipeline (`natgw/rtl/` & `cndm/board/Alveo/`)

#### Architectural Strengths
1. **Throughput & Clocking Margins:**
   All shim processing in [`natgw_shim.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_shim.sv) runs on `pcie_clk` (250 MHz) with 128-bit data paths per lane (32 Gbps/lane internal bandwidth vs. 10 Gbps MAC line rate). The central [`natgw_lookup.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_lookup.sv) pipeline accepts 1 key/cycle (250 Mpps), giving **2.1$\times$ over-provisioning** above the 8$\times$ 10GbE theoretical maximum of 119 Mpps (64B minimum frames).
2. **Non-Blocking DDR4 Second Tier:**
   In [`natgw_ddr.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_ddr.sv), each 64-byte AXI4 burst (`AWSIZE=6`, `AWLEN=0`, 512-bit bus) fetches an entire 2-slot DDR bucket in a single beat. Crucially, `natgw_ddr` tracks outstanding requests per lane (`MAX_OUT = 16`). If an ingress lane experiences a miss storm that saturates the 16 in-flight slots, additional misses **bypass DDR4** (`stat_skip`) and punt directly to VPP rather than stalling the lookup pipeline or overflowing the 16 KB per-lane [`hold_fifo_inst`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_shim.sv#L269-L286).
3. **Read-Modify-Write Hazard Avoidance in `natgw_state.sv`:**
   Because UltraRAM has a 4-cycle read latency (`RAM_PIPE = 3`), [`natgw_state.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_state.sv) includes a 4-stage bypass network forwarding in-flight counter/timestamp updates to back-to-back packets hitting the same flow index, plus a periodic bubble generator (`cfg_bubble_period`) to guarantee forward progress for host reads/writes and the background aging scanner.
4. **FLR (Function-Level Reset) Hardware Integration:**
   [`fpga_core_nat.sv`](file:///home/parallels/git/taxi/src/cndm/board/Alveo/fpga/rtl/fpga_core_nat.sv#L988-L1059) implements an explicit `flr_state_t` FSM (`FLR_IDLE` $\rightarrow$ `FLR_RESET` $\rightarrow$ `FLR_CLEAR` $\rightarrow$ `FLR_DONE`) that resets `cndm` and `natgw_shim`, waits for the hardware URAM clear to finish (`!nat_clear_busy`), and returns all lanes to bypass mode before pulsing `cfg_flr_done` to the PCIe hard IP. This makes VM reboots with VFIO passthrough clean and deterministic.

#### Identified Risks & Design Limitations
1. **Head-of-Line (HoL) Blocking Under Egress Port Oversubscription:**
   In [`natgw_shim.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_shim.sv#L424-L455), `egress_fifo_inst` is configured with `DROP_WHEN_FULL(0)`. If two 10G LAN lanes simultaneously forward hardware-offloaded traffic to a single 10G WAN lane at $>10\text{ Gbps}$ aggregate, `egress_fifo_inst` on that WAN lane fills and deasserts `tready` into [`natgw_switch.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_switch.sv). This backpressures `natgw_rewrite` and `hold_fifo_inst` on the ingress LAN lanes, **blocking frames on those LAN lanes destined for other uncongested WAN lanes as well as punted control frames**, until `ingress_fifo_inst` tail-drops indiscriminately at the MAC.
   - *Recommendation:* Consider setting `DROP_WHEN_FULL(1)` on `egress_fifo_inst` (with a per-lane egress tail-drop counter) so congestion on one egress port drops only frames targeting that port.
2. **Inability to Push or Pop 802.1Q VLAN Tags:**
   [`natgw_rewrite.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_rewrite.sv#L178-L180) patches bytes `0..63` in-place (`q_patch` / `q_mask`) and cannot shift packet bytes to insert or strip a 4-byte 802.1Q header. If `p1_res_reg.nh.vlan != p1_meta_reg.vlan`, the packet is punted with `RSN_VLAN`, and [`ngo_install()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L450-L451) refuses to offload the session (`NGO_CTR_SKIP_VLAN`).
   - *Implication:* The gateway cannot hardware-offload sessions between an untagged LAN port and a VLAN-tagged WAN trunk (or vice versa); both ingress and egress interfaces must either both be untagged or both be single-VLAN tagged.
3. **UDP Zero-Checksum Edge Case in `natgw_rewrite.sv`:**
   In [`natgw_rewrite.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_rewrite.sv#L199-L206), incremental L4 checksum calculation folds `p1_l4_sum_reg` via `csum_fold()`. Note that in [`natgw_pkg.sv`](file:///home/parallels/git/taxi/src/natgw/rtl/natgw_pkg.sv), `csum_sum3()` takes `~hc` (`16'hffff ^ hc`) and adds `~m + n`, and `csum_fold()` returns `~fold`. Because `csum_fold()` already inverts a folded `16'hffff` to `16'h0000`, line 201 (`if (!p1_meta_reg.tcp && d_l4_csum == 16'd0) d_l4_csum = 16'hffff;`) correctly converts a computed UDP checksum of `0x0000` to `0xffff` per RFC 768.

---

### Layer 2: Verification & Reference Models (`natgw/tb/` & `natgw/sw/model/`)

#### Strengths
- **Tri-Level Golden Co-Design:** Every stage of the hardware pipeline has a bit-accurate counterpart in both Python ([`tb/natgw_model.py`](file:///home/parallels/git/taxi/src/natgw/tb/natgw_model.py)) and C ([`sw/model/natgw_model.c`](file:///home/parallels/git/taxi/src/natgw/sw/model/natgw_model.c)). The C model backs a virtual DPDK PMD ([`natgw_model_pmd.c`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/model/natgw_model_pmd.c)), allowing the entire VPP + `natgw_offload` + `rte_flow` software stack to be tested in CI without FPGA hardware.
- **Thorough Corner-Case Coverage:** As documented in [`VERIFICATION.md`](file:///home/parallels/git/taxi/src/natgw/VERIFICATION.md), the cocotb suites explicitly test URAM RMW bypass hazards (back-to-back hits at distances 1–4 cycles), RFC 1624 `0x0000`/`0xffff` checksum boundaries, DDR4 request-cap saturation (`MAX_OUT`), AXI `RRESP` read errors (`SLVERR`/`DECERR`), and hitless live cuckoo relocations.

---

### Layer 3: Userspace HAL & Cuckoo Table Engine (`libnatgw`)

#### Strengths
1. **Hitless Make-Before-Break Cuckoo Relocation & Tier Migration:**
   In [`natgw_table_insert()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L824-L844), when a BFS relocation path `found <- p1 <- ... <- root` is executed, the emitted `ops[]` array writes each displaced entry to its new slot **before** overwriting its old slot. Similarly, [`natgw_tier_promote()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L478-L498) writes the on-chip URAM entry before deleting the DDR entry, and [`natgw_tier_demote()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L500-L530) writes the DDR entry, verifies `ds.active` is still true, and only then deletes the on-chip entry. Because on-chip hits take precedence over DDR hits, packets in flight **never experience a transient miss** during cuckoo relocations or tier migrations.
2. **Next-Hop Deduplication & Zero-Index Guard:**
   [`natgw_nh_get()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L883-L905) reference-counts identical next hops (`NATGW_NH_COUNT = 1024`) and reserves index `0` as invalid so a zeroed entry can never accidentally reference a valid next hop.

#### Improvement Opportunities
1. **Bit-by-Bit Packing & Bit-Serial CRC32 Overhead on Control Plane:**
   In [`natgw.c`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L16-L30), [`put_bits()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L16) and [`get_bits()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L24) loop 1 bit at a time (224 iterations per entry pack/unpack), and [`natgw_key_crc()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L143-L157) computes CRC32C and CRC32 bit-serially (112 iterations per hash, called up to `2 * 4^6` times during a depth-6 BFS cuckoo search when the table is near capacity).
   - *Recommendation:* Replace bit-by-bit `put_bits`/`get_bits` with word-aligned shifts/masks, and use hardware `_mm_crc32_u64`/`_mm_crc32_u32` (SSE4.2) for `NATGW_POLY_CRC32C` and a 256-entry lookup table (or PCLMULQDQ) for `NATGW_POLY_CRC32`. This will cut CPU time during high-load cuckoo BFS searches by $>10\times$.

---

### Layer 4: DPDK PMD & `rte_flow` Backend (`natgw_flow.c` & `cndm_natgw.c`)

#### Strengths
1. **Activity Word Pre-Clearing on DDR Slot Reuse:**
   In [`flow_hook()`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c#L710-L742), before writing a new flow into an empty DDR slot, `ddr_read_word()` reads and clears the 64-bit activity word containing that slot and credits any set bits to existing flows in that 64-slot block. This prevents a stale activity bit left behind by a previously deleted flow from falsely keeping a newly inserted flow alive.
2. **Adaptive Promotion Threshold Under DDR Contention:**
   In [`ddr_sweep_locked()`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c#L1089-L1101), if hardware `ddr_skips` are rising (`ctx->skips_rising`), the promotion threshold `promote_k` (out of `promote_n` sweeps) is automatically lowered (`k = k > 2 ? k - 2 : 1`) to pull active flows onto the chip faster and relieve DDR4 bandwidth pressure.

#### Critical Performance Bug / Bottleneck Found
> [!WARNING]
> **Excessive PCIe BAR0 MMIO Polling for `idle_pending` Flows in [`natgw_flow_poll()`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c#L1264-L1267)**
>
> In [`write_thresholds()`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c#L130-L139), when the `balanced` tier policy is active (`demote_idle = 10s`), the hardware idle scanner threshold (`THRESH_TCP`, `THRESH_UDP`) is set to `min(flow_age_timeout, demote_ticks)` = **10 seconds**.
>
> When an on-chip TCP flow (whose `age_timeout` is typically **7,440 seconds**) is idle for 10 seconds:
> 1. Hardware emits `NATGW_EVT_IDLE` and sets the flow's hardware `evp` (`event_pending`) bit to `1`.
> 2. [`natgw_flow_poll()`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c#L1234-L1249) pops the event, sees the flow has not reached its 7,440s `age_timeout`, and sets `f->idle_pending = true` and `f->pending_ts = s.ts`.
> 3. If the on-chip table is below its low-water mark (`onchip_low = 70%`, i.e., $<179,200$ flows), the flow is **not demoted** to DDR and remains on-chip with `f->idle_pending = true`.
> 4. On **every subsequent call to `natgw_flow_poll()`** (lines 1264–1267):
>    ```c
>    TAILQ_FOREACH(f, &ctx->flows, next) {
>        if (!f->ddr && (f->idle_pending || ctx->resync) && check_age_locked(ctx, f, now))
>            notify[f->e.key.lane] = true;
>    }
>    ```
>    [`check_age_locked()`](file:///home/parallels/git/taxi/src/natgw/sw/dpdk/common/natgw_flow.c#L1044-L1058) unconditionally calls `read_flow_state()` $\rightarrow$ [`natgw_dev_read_state()`](file:///home/parallels/git/taxi/src/natgw/sw/lib/natgw.c#L300-L309), which performs **2 PCIe BAR0 MMIO writes + 5 PCIe BAR0 MMIO reads** per flow while holding `ctx->lock`!
>
> **Impact:** If 50,000 established TCP flows sit idle for $>10\text{ s}$ (common on an ISP/enterprise gateway), every call to `natgw_flow_poll()` will execute **350,000 synchronous PCIe MMIO transactions** under `ctx->lock`, stalling flow creation/destruction for tens of milliseconds per poll.
>
> **Fix:** A flow with `f->idle_pending == true` cannot possibly reach its `age_timeout` before `(uint32_t)(now - f->pending_ts) >= (uint64_t)f->age_timeout * ctx->cfg.ticks_per_sec` (and if a packet hits the flow in the meantime, hardware automatically clears `evp = 0` and will emit a fresh `NATGW_EVT_IDLE` if it goes idle again). Guarding `check_age_locked()` with:
> ```c
> if (!f->ddr && (ctx->resync || (f->idle_pending &&
>     (uint64_t)(uint32_t)(now - f->pending_ts) >= (uint64_t)f->age_timeout * ctx->cfg.ticks_per_sec)))
> ```
> eliminates $>99.9\%$ of PCIe MMIO state reads during normal operation.

---

### Layer 5: VPP Dataplane & `natgw_offload` Plugin (`natgw/sw/vpp/`)

#### Strengths
1. **Minimal-Intrusiveness VPP Patches:**
   [`0001-nat44-ed-session-event-hooks-for-hardware-offload.patch`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/patches/0001-nat44-ed-session-event-hooks-for-hardware-offload.patch) adds only 120 lines to VPP's `nat44-ed` plugin, using a single `SNAT_SESSION_FLAG_OFFLOAD` bit and per-worker event callbacks (`ESTABLISHED`, `ACTIVE`, `CLOSING`, `DELETE`, `CLEAR`). Worker threads enqueue events into per-thread spinlock-protected vectors (`ngm->threads[ti].events`), keeping worker datapath overhead negligible.
2. **Barrier-Free Hardware Querying in [`ngo_sync()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L806-L859):**
   `ngo_sync()` performs all `rte_flow_query()` calls (which read FPGA registers over PCIe) on the main thread **before** acquiring `vlib_worker_thread_barrier_sync(vm)`, and holds the worker barrier only while updating `snat_session_t` counters and LRU timestamps in RAM.
3. **Automatic Next-Hop Revalidation:**
   After each sync batch, [`ngo_revalidate()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L719-L744) re-runs [`ngo_resolve_session()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L232-L252) against VPP's current FIB and adjacency tables; if a gateway MAC or ECMP path changed, it tears down and reinstalls the `rte_flow` rules automatically.

#### Improvement Opportunities
1. **Unnecessary Full-Pool Iteration in [`ngo_sync()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L760-L762):**
   At the start of every `ngo_sync()` (once per second), the code walks the entire `ngm->sessions` pool (potentially 1M+ sessions) just to populate `u16 ports[RTE_MAX_ETHPORTS]`:
   ```c
   pool_foreach (ngs, ngm->sessions)
     if (ngs->flow[0])
       ports[ngs->in_port[0]] = ports[ngs->in_port[1]] = 1;
   ```
   Maintaining a bitmask or per-port refcount of active ports on flow install/destroy avoids scanning up to millions of pool entries every second.
2. **Unconditional Linear Search of `ngm->pending` in [`ngo_session_free()`](file:///home/parallels/git/taxi/src/natgw/sw/vpp/natgw_offload/natgw_offload.c#L536-L537):**
   `vec_search(ngm->pending, pi)` is called for every freed session, even when `ngs->flow[0] != NULL` (which means the session was already installed and cannot be in `ngm->pending`). Guarding `vec_search` with `if (!ngs->flow[0])` avoids an $O(N)$ scan on normal session deletions.

---

### Layer 6: Control Plane (`wanmgr`) & Observability (`natgw_monitor`)

#### Strengths
1. **Self-Healing Stateless Reconciliation in `wanmgr`:**
   [`WanManager.round()`](file:///home/parallels/git/taxi/src/natgw/sw/wanmgr/wanmgr/manager.py#L193-L219) reconciles VPP's DHCP clients, `/32` probe routes, NAT44 address pool, and ECMP default route on every cycle, and resets its view upon VPP reconnect (`_on_connect`). When a WAN fails while another WAN is healthy, [`_flush_pending()`](file:///home/parallels/git/taxi/src/natgw/sw/wanmgr/wanmgr/manager.py#L342-L358) deletes and re-adds the downed WAN's NAT pool IP, immediately flushing all `nat44-ed` and FPGA-offloaded sessions bound to that WAN IP so clients fail over cleanly.
2. **Purpose-Built Hybrid Storage in `natgw_monitor`:**
   [`Store`](file:///home/parallels/git/taxi/src/natgw/sw/monitor/natgw_monitor/store.py) pairs **DuckDB** (columnar, integer-scaled 1s samples for 30 days + 1m min/avg/max rollups and counter snapshots for 365 days in ~410 MB total) with **SQLite** in WAL mode for transactional event logs and ISP outage records with 60-second pre-outage traffic snapshots.

#### Subtle Operational Risks
1. **Probe Target `/32` Routes in FIB Table 0 Can Blackhole Client Traffic to Those IPs:**
   [`_pin_probes()`](file:///home/parallels/git/taxi/src/natgw/sw/wanmgr/wanmgr/manager.py#L245-L256) installs a `/32` route in FIB table 0 (`0.0.0.0/0`'s table) for each WAN's `probe_targets` pointing to that WAN's gateway, and **keeps that `/32` route installed even when the WAN is `DOWN`** (so `wanmgr` can keep pinging it to detect recovery).
   - *Risk:* If an operator configures popular public DNS resolvers (e.g., `1.1.1.1` on `wan1` and `8.8.8.8` on `wan2`) as `probe_targets`, and `wan2` goes down, LAN clients querying `8.8.8.8` will match the `/32` route in table 0 and have their DNS queries routed into the dead `wan2` instead of failing over to `wan1`!
   - *Recommendation:* Either document prominently in [`wanmgr.example.toml`](file:///home/parallels/git/taxi/src/natgw/sw/wanmgr/wanmgr.example.toml) that `probe_targets` must be dedicated canary IPs that LAN clients do not use directly, or pass `sw_if_index` / source address to VPP's `ping` API (or use a dedicated probe FIB table) so `/32` overrides in FIB table 0 are not needed.
2. **Sequential Blocking Ping Timeouts in `wanmgr`:**
   [`VppApi.ping()`](file:///home/parallels/git/taxi/src/natgw/sw/wanmgr/wanmgr/vpp.py#L152-L163) blocks for `timeout` (default `0.5s`) per unreachable target. With 4 WANs and 2 targets per WAN, a total blackout blocks the `wanmgr` loop for `4 * 2 * 0.5s = 4.0s` per round. This is acceptable at small WAN counts, but worth noting if scaling to many WANs/targets.

---

### Layer 7: System Integration & Deployment (`natgw/sw/deploy/`)

#### Strengths
- **End-to-End Reproducibility:** [`natgw-host-setup.sh`](file:///home/parallels/git/taxi/src/natgw/sw/deploy/host/natgw-host-setup.sh) and [`natgw-guest-setup.sh`](file:///home/parallels/git/taxi/src/natgw/sw/deploy/guest/natgw-guest-setup.sh) automate IOMMU/VFIO binding, Proxmox VM creation (`q35`, `host` CPU, 16 GB RAM, 1 GB hugepages, `viommu=intel`), live PCIe hot-reprogramming via [`natgw-fpga-hook.sh`](file:///home/parallels/git/taxi/src/natgw/sw/deploy/host/natgw-fpga-hook.sh), and systemd hardening (`ProtectSystem=strict`, non-root `natgw-monitor` user), backed by automated unit tests in [`deploy/tests/`](file:///home/parallels/git/taxi/src/natgw/sw/deploy/tests/test_host_scripts.py).
- **Hardware Watchdog & Health Checks:** [`natgw-card-check`](file:///home/parallels/git/taxi/src/natgw/sw/deploy/guest/natgw-card-check) verifies PCIe link width/speed (`Gen3 x16`) and BAR0 magic before VPP starts.
