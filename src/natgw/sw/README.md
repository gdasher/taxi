# natgw host software

Everything on the host side of the natgw shim, from register access up to VPP.
Each layer is tested against the C software model of the FPGA. The same model
is the RTL testbenches' scoreboard, and the RTL shim and system testbenches
program the simulated hardware through libnatgw itself.

For wiring, addresses and a ready-to-adapt VPP configuration, see the [natgw wiring diagram and config template](https://claude.ai/artifact/ND6dAJem8HD2SyUbQYS8a2).

```
VPP nat44-ed ──session events──▶ natgw_offload plugin (vpp/natgw_offload)
                                      │ rte_flow
                    ┌─────────────────┴──────────────────┐
              net_cndm (real U200)              net_natgw_model (vdev)
                    └──────── common_natgw (dpdk/common) ┘
                                natgw_flow: rte_flow backend
                                      │
                         libnatgw (lib, include/natgw.h)
                   register access, cuckoo placement, next hops
                                      │
               BAR0 + 0x800000   or   C shim model (model/, natgw_model.h)
```

| Directory | What it is |
| --- | --- |
| `include/natgw.h`, `lib/natgw.c` | libnatgw. Register and layout definitions, entry and state packing, hashes, the host copy of the cuckoo table (BFS placement, relocation-safe write order), and the next-hop table. |
| `include/natgw_model.h`, `model/natgw_model.c` | C model of the shim. It has the same register interface and packet behaviour as the RTL. |
| `dpdk/common` | `common_natgw` DPDK driver: the rte_flow backend shared by both PMDs, plus punt metadata (mbuf dynfield and dynflag) and xstats. |
| `dpdk/model` | `net_natgw_model` vdev: the C model behind one ethdev port per lane. The wire is either rings with a test API (`wire=queue`) or TAP interfaces (`wire=tap`). |
| `../../cndm/dpdk/cndm` | cndm PMD changes: detects the NAT block, binds ports to lanes, provides flow ops and xstats, and strips the punt header on RX. |
| `dpdk/setup_dpdk.sh` | Adds the three drivers to a DPDK source tree. |
| `vpp/patches` | VPP patches. nat44-ed gains session event hooks; the dpdk plugin gains driver names, exports `dpdk_main`, and builds against DPDK 26.03. |
| `vpp/natgw_offload` | VPP plugin that mirrors nat44-ed sessions into the FPGA. |
| `vpp/setup_vpp.sh` | Applies the patches, links the plugin in, and builds VPP against the system DPDK. |
| `wanmgr` | WAN manager daemon (Python, `vpp_papi`): DHCP leases, health probes, default-route steering, NAT pool and session flushing, and email and syslog alerts. Includes a systemd unit and an example configuration. |
| `monitor` | Monitor daemon (Python, stdlib plus `vpp_papi`'s stats reader): a web page and JSON API over HTTPS with the gateway's statistics and the ISPs' state, alerts, and a persistent log of ISP outages. |
| `deploy` | Deployment on Proxmox VE: host and guest setup scripts, templates, switch configuration. See `deploy/README.md`. |

## Building

```
make                                    # libnatgw (build/libnatgw.{a,so})
dpdk/setup_dpdk.sh ~/src/dpdk           # then build DPDK, see below
vpp/setup_vpp.sh ~/src/vpp              # VPP installed to ~/src/vpp/install-natgw
```

Configure DPDK with:

- `-Denable_driver_sdk=true`, because VPP's dpdk plugin needs the driver headers.
- `-Ddisable_drivers=mempool/cnxk,mempool/dpaa,mempool/dpaa2,mempool/octeontx,mempool/bucket`.

The second option matters for shared builds. A shared DPDK autoloads every mempool driver, and together they fill the 16-entry mempool ops table. VPP's own ops then fail to register and VPP crashes on its first packet.

## Tests

| Suite | Command | Covers |
| --- | --- | --- |
| libnatgw and C model | `make test` (`tests/test_{layout,table,model,dev,ddr}.py`) | <ul><li>Layouts are bit-exact with the Python data types; hashes match the CRC written out from the spec.</li><li>Cuckoo placement stays relocation-safe after every write, for both the on-chip and the DDR table geometry.</li><li>The C model's forwarded frames, decoded with scapy, carry exactly the rewrite of the entry and next hop that hit (addresses, ports, MACs, VLAN, TTL, valid checksums), also with flows split between the two tiers; punts are unchanged; every punt reason is reachable.</li><li>Registers, events, aging and overflow; DDR status when absent, present without a DIMM and working; DDR entry access, clear and the activity bitmap.</li></ul> |
| DPDK rte_flow | `tests/test_dpdk_pmd.py`, which runs `dpdk/tests/test_natgw_pmd.c` on `net_natgw_model` | <ul><li>Validate rejects.</li><li>SNAT and DNAT rewrite, TTL and checksums, including UDP with a zero checksum.</li><li>Punt metadata.</li><li>COUNT, AGE and the aged-flow event.</li><li>Duplicates, filling the table to capacity, host TX, and devices without punt headers.</li><li>A concurrent datapath.</li><li>DDR tier: spill to DDR once on-chip is full, DDR punts carry the DDR index flag, no counters for DDR flows, DDR aging from the activity bitmap; with no DIMM or `no_ddr=1`, ENOSPC at on-chip capacity and no DDR lookups.</li><li>Tier policy: admission at the high-water mark and bulk flows to DDR; idle flows demoted and busy DDR flows promoted, with a probe frame run through the model after every single table command forwarding unchanged throughout; no ping-pong once settled; counters monotonic across demote and promote; `fill` ignores the watermark.</li><li>The flow context lock under load (`lock_stress`, three threads for 3 s): the main thread creates, destroys and queries flows (about 370k each of creates and destroys) while a poll thread runs the policy (over 1,000 promotions and demotions) and a traffic thread checks that every frame of a rotating set of busy flows is forwarded with its exact rewrite (about 2.7M frames). Then flow accounting, no migration failures, every flow forwarding, and a create never waiting more than 20 ms (about 0.2–0.3 ms seen). Moving without the lock crashes it; leaving destroyed flows on the candidate queues hangs it.</li></ul> |
| VPP integration | `pytest vpp/tests` (VPP and passwordless sudo; skipped otherwise) | Kernel TCP/UDP from namespaces on the model's TAP lanes, through VPP and the model. See below. |
| WAN manager | `pytest wanmgr/tests` | <ul><li>Unit tests against a fake VPP that models routing and NAT for probes: thresholds, flapping, target fallback, steering, flushing, lease changes, restarts, alert rate limiting and SMTP; the status file's per-WAN detail (why and since when a WAN is down: probe failure, link down, admin down, no lease; probe history and per-target results; event log; written on VPP loss).</li><li>Integration tests with real VPP, the offload plugin, dnsmasq "modems" and an SMTP sink (`tests/site_env.py`, shared with the monitor's tests): startup, an ISP outage, all WANs down, and a VPP restart.</li></ul> |
| Monitor | `pytest monitor/tests` | <ul><li>Collector against fake sources: rates (32-bit wrap, counter resets), offload ratio and punt reasons, table fill and DDR state, every alert (raise, hold, clear), ISP outages with their evidence snapshot, the event log and outage history across monitor restarts, a stale or missing WAN manager; the store (tiers and rollup values at their integer scales, minute counter snapshots, batching, a minute rewritten after a restart, retention that keeps open outages, a crash between flushes, readers during writes, reopening after a fatal DuckDB error, importing the old log, the daily growth) and graphs from the database after a restart; the stats reader (interface names with slashes, a hung VPP, a restarted VPP whose socket reuses the inode, a segment stuck mid-update).</li><li>The HTTPS server: TLS 1.2 minimum, plain HTTP refused, certificate reload, security headers, read-only methods, binding to an address only, to an interface name, waiting for an interface's address, a stalled client not blocking others, `--check`.</li><li>Integration with real VPP, the WAN manager and two ISPs: counters equal VPP's own (`show natgw offload`, xstats), per-ISP traffic, an ISP outage captured with its reason and evidence and kept across a monitor restart, all ISPs down, a VPP restart, graphs and counter history surviving a monitor restart, and the page in headless Chromium when Playwright is installed (light and dark, desktop and phone width: no script errors, no horizontal scrolling, the bundled typeface loaded, every window, a table view, the keyboard crosshair, a counter graph).</li></ul> |

The VPP integration tests run VPP with `net_natgw_model,wire=tap`. Each lane's TAP is moved into a namespace: one LAN and two WANs, each WAN with a server behind it. A test decides whether traffic was offloaded from what it can observe. Once a flow is offloaded, VPP's interface counters stop growing while its session counters keep growing, because they are synced back from the hardware.

The tests cover:

- **Offload triggers:** UDP after the ACTIVE threshold, and TCP after its handshake, never before.
- **Removal:** on FIN or RST, on nat44 session delete, on clear, and on disable.
- **Expiry:** an idle session expires and its flows are removed. Traffic seen only by the hardware keeps a session alive.
- **Next hops:** a next-hop MAC change reinstalls the flows.
- **ECMP:** across two WANs the hardware picks the same WAN as VPP, which the replies prove.
- **Table full:** sessions that don't fit stay in software and keep working.
- **DDR tier** (`test_offload_ddr.py`): with a working DDR tier every session is offloaded beyond the on-chip capacity; hardware-only traffic on DDR flows (which have no counters) keeps their sessions alive, and idle ones expire; with the tier present but no DIMM, extra sessions stay in software; `bulk-udp-port` sessions go to DDR; a busy session arriving while the chip is full of idle ones is promoted without losing a packet.
- **Observability:** the CLI and the stats segment gauges.

Fault-injection checks confirm the suites detect real bugs:

- libnatgw: mutations of the table code.
- DPDK: TTL decrement ignored, per-flow age ignored, MACs swapped, a promotion that clears the DDR copy before writing the on-chip one.
- VPP plugin: ECMP hash ignored, counter refresh skipped, CLOSING ignored, revalidation skipped, DDR activity refresh skipped.
- WAN manager: probes not pinned, no flush, steering before a verdict, DOWN after one bad round, and removing a down WAN's pool address.

## natgw_offload in brief

- **Session events.** nat44-ed calls the plugin's callback on the thread that owns the session:
  - **ESTABLISHED:** the TCP handshake is complete.
  - **ACTIVE:** the session has reached N packets (UDP).
  - **CLOSING:** a FIN or RST was seen.
  - **DELETE** and **CLEAR.**

  The callback copies the session into a per-thread queue. A process on the main thread drains the queues and owns every rte_flow call.
- **Flows per session.** Each session becomes two flows. Each flow enters on the port where the other direction leaves.
- **Next-hop resolution.** Next hops come from VPP's FIB, mirroring `ip4-lookup` and `ip4-load-balance`, including the flow hash used for ECMP. The i2o direction hashes the untranslated header, as output-feature NAT does. The destination and source MACs come from the adjacency rewrite, and so does the VLAN tag.
- **Sync.** Every `sync-interval`, and immediately for flows the hardware reports as aged, the plugin:
  - reads COUNT and AGE for each flow;
  - folds the packets, bytes and last-hit time into the VPP session under the worker barrier;
  - deletes sessions that VPP would have expired;
  - re-resolves next hops, and reinstalls any flow whose next hop changed.
- **When a flow can't be installed.** If there is no neighbour yet or the table is full, the session is retried each second, up to `max-retries` times. A session that can't be expressed in hardware stays in software:
  - twice-NAT, hairpinning or bypass sessions;
  - ICMP;
  - VLAN tagging that differs between the two sides.
- **Configuration.**
  - In `startup.conf`: `natgw-offload { enable active-packets 4 sync-interval 1 }`.
  - CLI: `natgw offload [enable|disable] ...`, `show natgw offload [sessions]`, `natgw offload sync`, `clear natgw offload counters`.
  - Stats segment: `/natgw/offloaded`.

### DDR tier

On bitstreams with the DDR tier (`DDR_STATUS` present) the rte_flow backend
uses it automatically when the memory calibrated:

- **Placement (tier policy).** With `tier_policy=balanced`, the default when
  a DDR tier is in use:
  - **Admission.** New flows go on chip while it is below `onchip_high`
    (85%) full, else to DDR.
  - **Demotion.** Idle on-chip flows (no hit for `demote_idle`, 10 s) move
    to DDR while the chip is above `onchip_low` (70%). The hardware idle
    scanner finds them, so the host never scans on-chip state.
  - **Promotion.** DDR flows seen active in `promote_k` of the last
    `promote_n` bitmap sweeps (6 of 8; 4 of 8 while DDR skips are rising) move
    on chip, into free room or in place of an idle flow.
  - **Limits.** At most `migrate_budget` (64) moves per poll, in batches of 8
    so flow creates aren't held up; a flow stays `min_residency` (30 s) in a
    tier before moving again.
  - **Hitless moves.** The new copy is written before the old one is
    cleared; on-chip lookups win, so the flow hits throughout. A demotion is
    abandoned if the DDR tier stops being looked up. The moves are
    libnatgw's `natgw_tier_promote` and `natgw_tier_demote`, so the RTL
    system test exercises the same code.
  - **Locking.** One spinlock per device (`ctx->lock`) covers the host
    tables, the index maps and key hash, next hops, the flow list and
    candidate queues, per-flow state, and every multi-register device
    sequence. The rte_flow calls, the poll and xstats take it; the packet
    path never does. The poll does its moves in batches of 8, releasing the
    lock in between, so flow creates wait for at most one batch.
  - **Counters.** On-chip counts are carried across a stay in DDR, so COUNT
    totals never go backwards.
  - **Bulk hint.** rte_flow priority 1 places a flow in DDR and never
    promotes it. The VPP plugin sets it for UDP to the server ports listed
    with `bulk-udp-port <port>` (repeatable; none by default).

  `tier_policy=fill` keeps first-come placement: on chip until the table
  refuses a flow, then DDR, with no moves. Without a usable tier (not built
  in, no DIMM, failed clear, or disabled), a full on-chip table gives ENOSPC
  as before and VPP keeps the session in software.
- **Start-up.** The context clears the DDR table and bitmap and then enables
  DDR lookups; a tier that is present but not calibrated is left disabled
  and logged.
- **Counters and aging.** DDR flows have no hardware counters: a COUNT query
  succeeds with `hits_set = bytes_set = 0`. AGE uses the activity bitmap:
  each poll reads up to 4096 occupied words, a set bit marks its flow seen,
  and flows are aged when a sweep completes, so idle times are accurate to
  one sweep. The VPP plugin refreshes a session's last-heard time from AGE
  for flows without counters.
- **Controls.** cndm PMD devarg `natgw_ddr=0` disables the tier; the model
  PMD takes `ddr_bucket_w=<5-20>`, `ddr_calib=0|1` and `no_ddr=0|1`. Both
  take the policy options `tier_policy`, `onchip_high`, `onchip_low`,
  `demote_idle`, `promote_k`, `promote_n`, `migrate_budget` and
  `min_residency`.
- **xstats.** `natgw_ddr_flows`, `natgw_ddr_lookups`, `natgw_ddr_hits`,
  `natgw_ddr_skips`, `natgw_ddr_read_errors` (these four device-wide,
  wrapping at 32 bits; a DDR read that returns an error never hits),
  `natgw_promotions`, `natgw_demotions`, `natgw_migrate_failures`,
  `natgw_onchip_load_pct`, `natgw_ddr_skips_last_sweep`.

### VPP NAT configuration for multiple WANs

The [wiring diagram and VPP configuration template](https://claude.ai/artifact/ND6dAJem8HD2SyUbQYS8a2) shows the whole deployment: switch VLANs, U200 lanes, VPP interfaces, and the gateway VM, with every port's IP and MAC address. It also covers the simulation test bed.

This differs from the configuration sketch in the scope of work. With two WANs (ECMP), nat44-ed needs two changes:

1. **Use NAT only as an output feature on each WAN, with no inside interface.** With `set interface nat44 in lan0`, the session is created at LAN input, before the route lookup. Its address therefore ignores the egress WAN. Later packets are also translated before the lookup, so their ECMP hash covers the translated source address and can pick the other WAN, still carrying the first WAN's address.
2. **Add pool addresses with a VRF (`nat44 add address <wan-ip> tenant-vrf 0`), not with `nat44 add interface address`.** An interface-derived address has no VRF, so `nat_ed_alloc_addr_and_port` never matches it to its egress interface. The WAN manager must therefore add and remove these addresses as DHCP leases change.

```
set interface nat44 out wan1 output-feature
set interface nat44 out wan2 output-feature
nat44 add address <wan1 lease> tenant-vrf 0
nat44 add address <wan2 lease> tenant-vrf 0
```

The WAN manager adds the pool addresses: it keeps each WAN's current lease in the pool with `tenant-vrf 0`, so `startup.conf` should not configure them.

## WAN manager in brief

`python3 -m wanmgr -c /etc/natgw/wanmgr.toml` (see `wanmgr/wanmgr.example.toml` and `wanmgr/natgw-wanmgr.service`). Each round (default every 2 s), the manager reconciles VPP with what the WANs' leases and health call for:

- **DHCP clients.** Each WAN interface has a DHCP client. The manager creates any that are missing, and reads the leases with `dhcp_client_dump`.
- **Pinned probe targets.** Each probe target has a /32 route via its own WAN's gateway, so a ping from VPP tests that WAN only.
- **Probing.** Each round pings a WAN's targets in order until one replies. A WAN goes DOWN after 3 failed rounds and back UP after 5 good ones. After a start or restart, 1 good round is enough. A WAN that has lost its lease is DOWN at once.
- **Steering.** The API-sourced default route, which outranks DHCP's, holds the healthy gateways and is always replaced as a whole set. When no WAN is healthy, the override is removed and DHCP's routes over every WAN take over. Nothing is steered until every leased WAN has a verdict.
- **NAT pool.** Every leased WAN address stays in the pool. VPP's own probes pass through NAT's output feature, so a WAN whose address was removed could never be probed back up.
- **Flushing.** When a WAN goes down and another WAN is healthy, its sessions and offloaded flows are flushed by removing its address and re-adding it at once. A changed lease replaces the old address, which also deletes the sessions using it.
- **Alerts.** Email is sent for DOWN and UP changes, lease loss and changes, and VPP disconnects. Mail is rate-limited, and alerts held back are summarised in the next mail. Every alert also goes to syslog.
- **Recovery after a VPP restart.** The manager reconnects with backoff, relearns the leases quietly, and probes before re-applying anything.

## Monitor in brief

`python3 -m natgw_monitor -c /etc/natgw/monitor.toml` (see `monitor/natgw_monitor/config.py`; the deployment installs it as `natgw-monitor.service`). It only reads, so it can't disturb the datapath or rule installs:

- **VPP's stats segment** (`/run/vpp/stats.sock`, group `vpp`):
  - the device's xstats, which VPP's dpdk plugin copies there every `stats-poll-interval` (1 s in the deployment): per-port forwarded and punt-reason counters, flow counts and capacities, DDR status and counters, tier moves, events;
  - the offload plugin's counters (`/natgw/offload/*`), the port link states (`/natgw/link/<if>`), and a heartbeat (`/natgw/heartbeat`);
  - VPP's interface, NAT and vector-rate counters.
- **The WAN manager's status file** (`/run/natgw-wanmgr.json`): each ISP's state, why and since when, lease, link, probe history and per-target results, whether it is in the default route, and recent events.

Every second it computes rates and evaluates alerts. When an ISP goes down it records an outage with the evidence at that moment, and closes it when the ISP is back.

History is stored locally (`natgw_monitor/store.py`), so it survives restarts. Statistics are in DuckDB (`/var/lib/natgw-monitor/stats.duckdb`); events and outages are in SQLite (`monitor.db`):

| Tier | Resolution | Kept for | Size (measured) |
|---|---|---|---|
| Graph series (traffic, punt reasons, tables, tier moves, per-ISP inbound) | every 1 s sample | 30 days | ~80 MB |
| The same series as min/avg/max | 1 minute | 1 year | ~64 MB |
| A snapshot of every counter (device xstats, VPP interfaces, offload, NAT) | 1 minute (`counter_minutes`) | 1 year | ~270 MB |
| Events and outages (SQLite) | each one | 1 year (open outages always) | a few MB |

That's about 410 MB of data. DuckDB's file also carries free space that it reuses as old data is pruned, so plan for 1 GB of disk. All retentions are settings (`[store]`).

**Why DuckDB for statistics.** It compresses each column and scans only the columns a graph needs, so long windows of fine data stay small and fast. Measured on monitor-shaped data:

| | SQLite | DuckDB |
|---|---|---|
| 30 days of 1 s graph data | 206–484 MB | 80 MB |
| A month's graph from 1 s data | 2.6–3.3 s | 23–44 ms |
| Every counter at 1 s for a week | 800 MB | 253 MB |

Values are stored as integers at a fixed scale per series: rates in whole units; percentages, small rates and up/down fractions in hundredths. Whole numbers compress 2–4 times better than full-precision floats. How noisy the real per-second rates are moves the 1 s tier by up to about 3 times.

**Costs of DuckDB:**
- **The lock.** Its file is locked to the monitor's process, so read statistics through the JSON API. Events and outages stay in SQLite so that `sqlite3` can read them while the monitor runs.
- **Memory.** Appends hold an uncompressed 256 KB block per column (about 300 columns, ~80 MB) until a checkpoint compresses them. The cache is capped at `memory_mb` (256), and the process peaks near 400 MB; a 64 MB cap made writes fail.
- **Fatal errors.** A fatal DuckDB error invalidates the connection. The store then reopens it, losing at most the batch being written.
- **The package.** `duckdb` is pinned (`requirements.txt`, and the setup script). Its Python binding probes for NumPy and pandas on every bound parameter, about 20 ms per write, so rows are written as literal integer values instead.

How it's stored:
- **Write pattern.** Samples are written in batches every 10 s, about 7 ms per batch. Both stores journal: a crash loses at most the last batch and corrupts nothing.
- **Pruning.** Data past its retention is deleted hourly, followed by a DuckDB checkpoint, so freed space is reused. In a three-week rolling test the file stayed flat.
- **Graphs.** Graphs choose the tier by window: 1 s data up to 30 days (unless the summaries reach further back), minute summaries beyond. Each point is a bucket average, with the bucket's peak drawn as a thin line.
- **Counters.** Any counter can be graphed over the year, from its snapshots: a rate between two snapshots is exact.

Also evaluated:
- **TinyFlux** needed 3.8 GB of memory and 9 s to open a week of 1 s data.
- **SQLite with a row per value**, or with JSON documents, was 3 to 3.3 times larger.

An `events.jsonl` from the first version is imported once and renamed `events.jsonl.imported`. Statistics from the SQLite-only version are dropped from `monitor.db`, not migrated; it was never deployed.

The page shows:
- a one-sentence verdict, and the signal path from the LAN through the gateway to each ISP. A down ISP is drawn cut: on its own link when the link or lease is lost, beyond the ISP when its probes go unanswered.
- alerts, each ISP's evidence (lease, link, route, probe rounds and targets), and current figures;
- graphs over 10 minutes, an hour, a day, a week, 30 days or a year. They show forwarded vs sent to VPP, offload share, punt reasons, per-ISP inbound with outages shaded, table fill, tier moves and DDR hit rate. Each graph has a crosshair (also by keyboard) and a table view.
- the outage history, the ports, the event log, and every counter, each of which can be graphed.

The page uses one bundled typeface, Atkinson Hyperlegible Next (SIL OFL, `static/fonts`), and nothing from outside: it works with every ISP down. `tests/preview.py` serves it on a synthetic week for work on the page.

The JSON API serves the same data:
- `/api/stats`;
- `/api/history?seconds=` (up to the retention, a year);
- `/api/events`;
- `/api/counters` and `/api/counter?name=&seconds=`;
- `/healthz`.

What it can't see: frames the FPGA forwards are counted per ingress lane, so an ISP's inbound traffic includes the hardware path but its outbound traffic is VPP's only.

Like VPP's own CLI, `vpp_papi` can't tell a gauge changed without a directory refresh. Its reads also wait without limit while VPP updates the segment, so the monitor refreshes on every read and bounds every read. A segment that stays unreadable (VPP died mid-update) counts as VPP down and is remapped.
