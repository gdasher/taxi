# NAT gateway shim: verification

This document lists what is verified for the natgw NAT fast path and how:
the formal properties proved with SymbiYosys, and the simulation tests run
with cocotb and Verilator. All of it runs on the RTL in `rtl/`; nothing here
has run on hardware yet.

Shared basis: `tb/natgw_model.py` is a bit-exact Python model of the shim
(classification, hashing, cuckoo placement, rewrite, checksums, punt header).
Simulation tests use it as the scoreboard. The formal harnesses do not use it;
they restate the specification in SystemVerilog (`formal/common/natgw_formal_ref_pkg.sv`)
so the two methods check against independent references.

## How to run

```
# simulation (cocotb + Verilator); each tb directory is one pytest module
cd src/natgw/tb/natgw_parser && pytest -n auto        # likewise natgw_lookup, natgw_state, natgw_rewrite, natgw_shim
cd src/cndm/board/Alveo/fpga/tb/fpga_core_nat && pytest

# formal (OSS CAD Suite: yosys with the slang plugin, sby, bitwuzla)
src/natgw/formal/run_formal.sh 3                      # all tasks, 3 at a time
src/natgw/formal/run_formal.sh 3 lookup rewrite       # selected harnesses
FORMAL_TIMEOUT=1200 src/natgw/formal/run_formal.sh 6  # cap each task at 20 minutes; a capped BMC
                                                      # reports the last cycle fully proved
```

## Formal verification

### Method and what "proved" means here

Each harness in `formal/<block>/` instantiates the unmodified RTL block,
drives every input with unconstrained values (subject only to the interface
rules: valid/ready stability, one reset), and asserts properties against a
shadow model or a specification function.

- **Full proof**: holds for every input sequence of any length. Obtained by
  k-induction (`mode prove`), or for combinational logic by checking the one
  time step with all inputs free.
- **Bounded proof (BMC)**: holds for every input sequence up to the stated
  number of clock cycles from reset. Depths were chosen to cover the block's
  full latency plus at least two complete frames or operations, with every
  stall pattern; a bug needing a longer sequence would not be found.
- **Cover**: a reachability check run alongside each harness, showing the
  properties are not vacuous (the interesting scenarios do occur).

Table sizes are reduced (for example 2 buckets per table in the lookup
harness, 8 entries in the state harness); the RTL is parameterised and the
same code is used at full size.

### Properties and results

Results from the final suite run (bitwuzla via SymbiYosys; each BMC task
capped at 20–30 minutes; "bound" is the number of clock cycles from reset for
which every assertion is proved, and "reached" is the earliest cycle at which
the property's main check can first fire, from the matching cover trace).

| Harness | Task | Properties | Result | Bound (cycles) | Main check reached at | Meaning |
| --- | --- | --- | --- | --- | --- | --- |
| `csum` | – | P1.1–P1.5 | **Proved** | all inputs (combinational) | – | complete |
| `fifo` | `prove` | P7.1–P7.3 | **Proved** (k-induction) | unbounded | – | complete |
| `parser` | `bmc` | P2.1–P2.4 | Bounded pass | 11 | both frames' descriptors at 8 | arbitrary frames of 1–80 bytes through descriptor and hold output, including back-to-back pairs of short frames |
| `rewrite` | `bmc` | P3.1–P3.5 | Bounded pass | 8 | punted frame with header at 8; forwarded frame at 10 | P3.1 and P3.2 (routing, order, statistics; punted frames and headers) checked; the forwarded-frame checks P3.3–P3.5 are not reached within this bound |
| `rewrite` | `data` (no stalls) | P3.1–P3.5 | Bounded pass | 8 | forwarded frame at 10 | as above; see note |
| `lookup` | `stable` (8 lanes), `lookup_stable2` (2 lanes) | P4.1, P4.2 | Bounded pass | 12 | first result at 11; first tracked result at 14 | P4.1 (latency, order, lane tagging, hit stream) checked for keys accepted from cycle 1; P4.2 not reached |
| `lookup` | `reloc` (8 lanes), `lookup_reloc2` (2 lanes) | P4.1, P4.3 | Bounded pass | 12 | first tracked result at 14 | as above; P4.3 not reached |
| `state` | `bmc` | P5.1–P5.3 | Bounded pass | 14 | read-back after three hits at 6 | host write, consecutive hits on the tracked index and read-back, with scanner and event traffic interleaved |
| `switch` | `bmc` | P6.1–P6.4 | Bounded pass | 15 | two frames each way at 8 | several frames through each output with contention |
| `tx_merge` | `bmc` | P8.1–P8.4 | Bounded pass | 18 | two frames per source at 9 | interleaved core and shim frames, completions |
| `ram` | 1 bank, PIPE 2 | P9.1–P9.2 | Bounded pass | 13 | – | repeated write/read sequences (a read of a written address completes from cycle 1 + 1 + PIPE) |
| `ram` | 8 banks, PIPE 3/4/5 | P9.1–P9.2 | Bounded pass | 12 / 13 / 14 | – | across one mux level, with and without bank and output registers |
| `ram` | 32 banks, PIPE 4/5/6 | P9.1–P9.2 | Bounded pass | 11 / 12 / 13 | – | across two mux levels |
| `actmap` | `p2_bmc`, `p3_bmc` (RAM_PIPE 2, 3) | P10.1–P10.3 | Bounded pass | 21 | read-back of a word with bits 0 and 63 set by hits | hits on every cycle the producer may send, host read-and-clear and bulk clear interleaved, both ends of the forwarding window (DDR branch) |
| `ddr` | `bmc` | P11.1–P11.5 | Bounded pass | 16 | a request-cap skip at 5; a DDR hit at 9 | two lanes of lookups against a free AXI memory (arbitrary stalls and data, in order per ID), calibration and enable toggling; covers several complete lookups per lane, the cap and a DDR hit (DDR branch) |
| all | `cover` | – | Pass | – | – | every harness reaches its checked scenarios |

Note on the rewrite: the forwarded-frame properties (P3.3–P3.5: unchanged
bytes, specified fields, header and L4 checksums) were not reached by BMC in
the time available: a forwarded frame needs at least three 16-byte beats, so
the first such check happens at cycle 10, beyond the bound of 8 reached with
two 80-byte symbolic frames. Those properties are covered by the full proof
of the checksum algebra (P1), by simulation (several thousand frames
including scapy checksum validation), and by the routing part (P3.1) here.
Closing the gap needs either more solver time or a harness restricted to a
single frame.

Note on the lookup: the exactness and relocation properties (P4.2, P4.3)
were not reached either. Even from empty tables, a tracked lookup completes
no earlier than cycle 14 (the 10-cycle lookup latency plus the setup of
the tracked key's slots), and each BMC step beyond 12 took over 25 minutes
with arbitrary 112-bit keys and CRC hashing every cycle, also with only
two active lanes. They are covered in simulation: `run_test_fill_lookup`
checks every result at 90% load, and `run_test_concurrent_writes` checks
that keys being relocated never miss.

### Properties in detail

**P1 Checksum algebra** (`formal/csum`, `natgw_pkg` functions). The message is
eight 16-bit words plus a checksum; up to three words change, as in the
rewrite (two address words and the TTL/protocol word, or two address words and
a port). With every value free:

- P1.1 a valid checksum stays valid after the incremental update (RFC 1624);
- P1.2 an invalid checksum stays invalid, so the receiver still drops it;
- P1.3 the incremental result equals a full recompute, up to the two encodings of zero;
- P1.4 replacing a computed UDP checksum of 0x0000 by 0xFFFF keeps the packet valid;
- P1.5 `csum_update3` equals `csum_fold(csum_sum3(...))`, the split used by the rewrite pipeline.

The proof assumes one unchanged non-zero word (the IPv4 version/IHL word, or
the protocol in the TCP/UDP pseudo header).

**P2 Parser** (`formal/parser`). Two frames of arbitrary content and length
(1 to 80 bytes) back to back, arbitrary idle cycles and backpressure:

- P2.1 every hold-output beat equals the input beat (data, keep, last, user);
- P2.2 one descriptor per frame, in order;
- P2.3 the descriptor's key, lookup/FIN/RST flags, length and every metadata field equal the specification classifier (all 13 punt reasons, their precedence, VLAN handling, header checks);
- P2.4 both outputs hold steady while stalled (AXI-Stream rules).

**P3 Rewrite** (`formal/rewrite`). Two frames of arbitrary content and length,
each with the metadata the parser produces for it and an arbitrary lookup
result; arbitrary configuration, stalls and backpressure:

- P3.1 a frame is forwarded exactly when the specification says so (reason MISS, hit, valid next hop, egress lane enabled, VLAN tagging matches); output order and per-frame statistics reason are correct;
- P3.2 punted frames are unchanged, tuser preserved, with a punt header of exactly the specified fields when enabled (never for bypass);
- P3.3 forwarded frames keep their length, go to the next hop's lane, carry the specified MACs, VLAN ID (priority bits kept), TTL and translated address and port, and every other byte is unchanged;
- P3.4 forwarded frames have a valid IPv4 header checksum;
- P3.5 the TCP/UDP checksum stays consistent for any payload: the one's-complement sum over the changed words (addresses, ports, checksum) is invariant; a UDP checksum of 0 stays 0 and a computed one is never 0.

**P4 Lookup engine** (`formal/lookup`). All eight lanes present arbitrary keys;
the host writes arbitrary entries and next hops. One key K and one next-hop
index N are tracked against a shadow of K's eight candidate slots:

- P4.1 every accepted key yields exactly one result after the fixed latency, with its lane; no other results; the hit stream fires exactly for looked-up hits with the same index;
- P4.2 (task `stable`) once K's slots and next hop are written and then left alone, K's result (hit, first-match index, action, next hop, hash) equals the specification; a frame not looked up gets no hit and hash 0;
- P4.3 (task `reloc`) while the host keeps the relocation protocol (at least one valid copy of K in its slots, every copy with the same action), writes may land anywhere including K's slots, and K always hits with that action: no miss during a relocation.

**P5 Per-entry state** (`formal/state`). Hits (any index, every cycle
possible), host writes and reads, the aging scanner and event backpressure
interleave freely; one index is tracked:

- P5.1 a host read returns exactly the shadow's valid, TCP, FIN, RST, packet and byte counts, so read-modify-write forwarding is exact including back-to-back hits on one entry;
- P5.2 host operations are never accepted in a cycle with a hit, and host reads return in order after RAM_PIPE+1 cycles;
- P5.3 every event has a valid type and an index inside the table.

**P6 Lane switch** (`formal/switch`, four lanes). Arbitrary frames with a
constant destination per frame, arbitrary output stalls:

- P6.1 every output beat came from a frame addressed to that output;
- P6.2 frames are never interleaved on an output;
- P6.3 per (input, output) pair, beats arrive in order with none lost or duplicated;
- P6.4 outputs hold steady while stalled.

**P7 Descriptor FIFO** (`formal/fifo`, full proof by k-induction):

- P7.1 pops return exactly the pushed values in order;
- P7.2 ready and the overflow flag are exact;
- P7.3 the output holds steady while stalled.

**P8 Transmit merge** (`formal/tx_merge`):

- P8.1 core frames leave with tid 0, shim frames with tid 1;
- P8.2 frames are not interleaved at the MAC;
- P8.3 each source's beats reach the MAC once, in order;
- P8.4 only tid-0 completions reach the core, all of them do, and other completions are always consumed.

**P9 Banked RAM** (`formal/ram`). `natgw_ram` against a plain single-array
memory with the same timing contract, for one bank, eight banks (one mux
level) and 32 banks (two mux levels), each at the minimum latency, with the
bank output register and with an extra output stage:

- P9.1 every port-A read of a written address returns the reference value after PIPE cycles;
- P9.2 the same for port-B reads, with port B also taking the writes.

**P10 Activity bitmap** (`formal/actmap`, DDR branch). Hits (set requests)
arrive whenever the producer's almost-full input allows, the host reads and
clears arbitrary words one at a time, and bulk clears start at any time; one
word W is tracked against a shadow:

- P10.1 a host read of W returns exactly the bits set in W since the last read or clear of W (read-modify-write forwarding exact for back-to-back updates of one word);
- P10.2 host reads return in order, RAM_PIPE+1 cycles after issue;
- P10.3 the set FIFO never overflows, so no hit is lost while the producer honours almost-full.

**P11 DDR lookup** (`formal/ddr`, DDR branch, `MAX_OUT` 2, `QUEUE_DEPTH` 4).
On-chip results arrive on two lanes, each a DDR candidate or not; the AXI
memory answers only outstanding reads, in order per ID, with arbitrary data
and stalls; calibration and the enable toggle freely. Lane T's results are
tracked through a shadow queue:

- P11.1 results leave in order per lane;
- P11.2 a result that was not a DDR candidate (not looked up, or an on-chip hit) leaves unchanged; a candidate leaves unchanged (a miss) or as a hit carrying the DDR index flag, with its hash unchanged;
- P11.3 never more than 2 x MAX_OUT reads outstanding per lane;
- P11.4 no DDR lookup is issued while the tier is inactive, and every lane read belongs to an issued lookup (two reads each);
- P11.5 the per-lane queues never overflow.

Both harnesses were checked against mutants: removing the bitmap's
forwarding from the most recent write fails P10.1 at once, and ignoring the
request cap fails P11.3 within three cycles. The bound for P11 is for the
compare-on-arrival version of the DDR stage (lines compared against the
lane's oldest key as they arrive): 16 cycles took 33 minutes (15 took 9), and
the mutant above still fails.

### Defects found by formal verification

| Block | Defect | Fix |
| --- | --- | --- |
| `natgw_parser` | The hold output could withdraw `tvalid` without a transfer when the descriptor stage stalled in the same cycle (AXI-Stream violation). The hold FIFO tolerated it, so simulation passed. | Hold output now goes through a skid register (commit `8e352c9`). |

Harness defects found and fixed while bringing the suite up (not design
defects): the slang frontend ignores `(* anyconst *)` (replaced by held
registers); partial array writes in `always_comb` created false combinational
loops; the switch harness first demanded a global per-source order the design
does not promise; parallel tasks of one `.sby` file raced on a shared status
database.

## Simulation tests

All tests below pass on the RTL of commit `1933cd5` (final regression, 2026-10-08). Each checks every output against the
reference model unless stated otherwise.

### Block tests (V1)

| Testbench | Test | What it covers |
| --- | --- | --- |
| `natgw_parser` | `run_test_reasons` | Every classification outcome (miss, bypass, not IPv4 incl. ARP/IPv6/double tag/QinQ, multicast, IP header errors incl. options/bad version/IHL/length, checksum, fragments MF and offset, TTL 0/1, ICMP, SYN, FIN, RST, SYN+FIN precedence), VLAN and untagged, runts 1–48 bytes, 9000-byte jumbos |
| | `run_test_backpressure` | The same frames under random stalls on input, hold output and descriptor output |
| | `run_test_throughput`, `run_test_throughput_cycles` | One beat per clock for back-to-back 1/16/17/60/64-byte frames; 256 back-to-back 60-byte frames in 1026 cycles (1024 ideal) |
| | `run_test_random` | 3000 mixed frames, half under backpressure |
| `natgw_lookup` (BUCKET_W/RAM_PIPE 6/2, 6/3, 12/4) | `run_test_fill_lookup` | Fill to 90% load, present and absent keys on all lanes, lookup=0 frames, FIN/RST pass-through, full result and hit-stream scoreboard |
| | `run_test_collisions` | Many keys in one bucket: overflow into T1 and relocation |
| | `run_test_concurrent_writes` | Load rising to 93% with continuous lookups on 64 probe keys relocated repeatedly: no miss, action unchanged |
| | `run_test_delete_update` | Deletes and in-place updates |
| | `run_test_host_read_clear` | Entry and next-hop reads; table clear (including next hops) |
| | `run_test_throughput` | 8 lanes saturated: 1.000 lookups per clock, exact round-robin fairness; bubble insertion |
| | `run_test_seeds` | Non-default hash seeds at 85% load |
| `natgw_state` (RAM_PIPE 2, 3, 4) | `run_hits_exact` | Hits with gaps of 0 to RAM_PIPE+2 cycles, alternating indices, bursts: counts, bytes and timestamps exact |
| | `run_host_rw` | Random host writes and reads |
| | `run_events_finrst` | FIN, RST, FIN+RST events and flags |
| | `run_idle_scan` | TCP/UDP idle thresholds, tick wrap, pending-event suppression, a hit clearing it |
| | `run_scan_rate` | Scanner pacing |
| | `run_fifo_full` | Event FIFO full: scanner pauses, FIN/RST losses counted, one overflow marker |
| | `run_clear` | Clear |
| | `run_random_mixed` | 6000 cycles of mixed hits, host operations and scanning against an op-level model |
| `natgw_ram` (1, 8, 32 and 64 banks; every PIPE from the minimum up to two spare stages) | `run_test_random` | 6000 cycles of random port A reads and port B reads and writes, with hot addresses for back-to-back same-address traffic, against a reference memory: read latency, read-before-write ordering and the bank decode and mux tree (small banks: `BANK_AW` 2) |
| `natgw_rewrite` | `run_test_directed` (18 variants) | SNAT/DNAT × TCP/UDP × VLAN/untagged × TTL decrement, UDP zero checksum, the 0x0000→0xFFFF case, invalid/missing next hop, disabled egress lane, VLAN mismatch, FIN/RST hit and miss, every punt reason; punt header on/off × idle × backpressure patterns |
| | `run_test_random` (8 variants) | 1500 mixed frames per variant; forwarded frames also checked by scapy |
| | `run_test_throughput` | 0.999 beats per clock for forwards and punts; one extra beat per header |

### Shim test (V2): `natgw_shim`, all eight lanes at their own clocks (RAM_PIPE 3 and 5)

| Test | What it covers |
| --- | --- |
| `run_test_bypass` | After reset every lane is in bypass: frames pass to the core unchanged, host transmit merged, only host completions returned |
| `run_test_nat` | Sessions installed over AXI-Lite; LAN→WAN and WAN→LAN forwarded and rewritten; misses and exceptions punted with headers; FIN on an offloaded flow; per-reason statistics and per-entry counters read back |
| `run_test_random` | Four bursts of 600 mixed frames on all lanes with random stalls, host transmit on the WAN lanes, table churn (deletes, inserts with relocation) between bursts |
| `run_test_line_rate` | Three lanes of 64-byte hits at 25G line rate with no drops; then an 8-lane flood beyond lookup capacity where every frame is either delivered or counted as dropped |
| `run_test_bad_frames` | Bad-FCS frames dropped at ingress and counted |
| `run_test_events` | FIN/RST events on hits; idle events from the scanner with one live flow kept active |

### DDR tier tests (DDR branch)

Block test `natgw_ddr` (DDR_BUCKET_W 6, MAX_OUT 4, activity-bitmap pipeline 2 and 3), against cocotbext-axi's `AxiRam` with random stalls on every channel:

| Test | What it covers |
| --- | --- |
| `run_test_host_access` | DDR entry write, clear and read through the host port; clearing one slot (a half-line write) leaves the other slot of its line intact; the bulk clear zeroes the whole table |
| `run_test_lookup_exact` | Low-rate lookups (no skips allowed): every DDR candidate hits or misses exactly as the Python `DdrTable` says, with the DDR index flag and next hop |
| `run_test_lookup`, `run_test_lookup_stalls` | All lanes at full rate with memory stalls: results in order per lane, every result either exact or a counted skip |
| `run_test_request_cap` | Slow memory: the per-lane cap holds and misses beyond it are skipped and counted |
| `run_test_activity` | After 1500 lookups at full rate, the bitmap holds exactly the DDR entries that hit; a read clears its word |
| `run_test_inactive` | Not calibrated, or disabled: no lane reads reach the memory |

Fault injection caught by these tests: wrong table-1 index, request cap
ignored, no bitmap forwarding.

Shim test `natgw_shim` with `DDR_ENABLE=1` (in addition to all tests above):
`run_test_ddr_random` (sessions split between the tiers, mixed traffic, activity
bitmap checked against the model), `run_test_ddr_fin_and_entries` (FIN on DDR
flows, entry read-back, statistics), `run_test_ddr_no_dimm` (calibration low:
DDR entries never hit, no memory traffic).

Host side: see `sw/README.md` (libnatgw and C model against the Python model
with both tiers, the DPDK rte_flow tiering tests, and VPP integration with and
without a DIMM).

### System test (V3): `fpga_core_nat`, PCIe, cndm driver and BASE-R SerDes models

| Test | What it covers |
| --- | --- |
| `run_test_bypass` | The unmodified stock cndm test passes through the shim |
| `run_test_nat` | Sessions installed through BAR0 by the driver model; hits forwarded wire to wire with nothing reaching the host; a miss and a FIN punted to the driver with a header; FIN event; host transmit merged with forwarded traffic on one lane; statistics, per-entry state and idle events read back |
| `run_test_flr` | Function-level reset: cfg_flr_done pulses once; the core and shim reset, tables and next hops cleared, lanes back in bypass, statistics zero; the driver re-initialises and traffic flows |

## Not covered

- Hardware: link bring-up, PCIe enumeration on a real host, real line rate.
- DDR tier on hardware: memory calibration with the real RDIMM, DDR latency
  and the resulting lookup rate (the simulations use a behavioural memory).
  The system testbench runs without the tier.
- Timing of the `fpga_AU200_nat_ddr` build. With 256k on-chip entries plus
  the tier (first full build, 2026-10-10) it routes but misses: WNS -0.234 ns,
  593 endpoints, in Ethernet MAC receive (390 MHz) and the 250 MHz core, with
  heavy routing congestion; no DDR-tier path failed. With 128k on-chip
  entries (now the variant's default) and 4M in DDR it **meets timing** (same
  day, RTL of commit `ae12f94`): WNS 0.000 ns, TNS 0, WHS +0.006 ns after
  post-route phys_opt; 169k LUTs, 176 URAM. The margin is zero, and routing
  took about two hours through congestion. With the compare-on-arrival DDR
  stage (commit `0cd6e71`; no 512-bit line buffers or 1024-bit merge) the
  same configuration routes in about 12 minutes with little congestion and
  meets timing with WNS +0.019 ns, WHS +0.008 ns (tightest: the 250 MHz
  core, +0.019 ns, and MAC receive on lane 1, +0.021 ns).
- Timing closure of the 512k-entry build. Build results:
  - 128k entries (`NAT_BUCKET_W=14`, commit `1933cd5`): meets timing on
    every clock (WNS +0.015 ns, WHS +0.010 ns).
  - 256k entries (`NAT_BUCKET_W=15`, the default), commit `42cb336`: meets
    timing on every clock after an extra post-route `phys_opt_design
    -directive AggressiveExplore` pass (WNS +0.005 ns, WHS +0.010 ns; -0.020
    ns before it). What closed it: keeping the Ethernet MAC/PHY in SLR2 next
    to the transceivers (`natgw_floorplan.xdc`; the first 256k build spread
    MAC receive logic across the SLR boundary around the URAMs, WNS -0.366
    ns) and per-bank RAM input registers. The state-stage retiming that
    followed adds margin; post-route phys_opt is now part of the build.
  - 512k entries (`NAT_BUCKET_W=16`): places and routes all 640 URAMs but
    misses timing (best WNS -0.718 ns, 7759 endpoints, before the changes
    above), mostly around the PCIe hard block in SLR1 and in the per-entry
    state update.
- Unbounded proofs for the parser, rewrite, lookup, state, switch, transmit merge and banked RAM (bounded to the depths in the table).
- Formal checks of the forwarded-frame rewrite (P3.3–P3.5) and of lookup exactness and relocation (P4.2, P4.3): their harnesses are written but the proofs did not reach the depth where these checks fire; simulation covers them.
- Cross-check of the model against VPP's own NAT (needs the software phase).
