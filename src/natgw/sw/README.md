# natgw host software

Everything on the host side of the natgw shim, from register access up to VPP.
Each layer is tested against a software model of the FPGA, and that model is
checked against the RTL's own scoreboard.

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
| libnatgw and C model | `make test` (`tests/test_{layout,table,model,dev}.py`) | <ul><li>Layouts and hashes are bit-exact with the Python model.</li><li>Cuckoo placement matches the Python model write for write, and stays relocation-safe after every write.</li><li>The C model matches the Python ShimModel byte for byte.</li><li>Registers, events, aging and overflow.</li></ul> |
| DPDK rte_flow | `tests/test_dpdk_pmd.py`, which runs `dpdk/tests/test_natgw_pmd.c` on `net_natgw_model` | <ul><li>Validate rejects.</li><li>SNAT and DNAT rewrite, TTL and checksums, including UDP with a zero checksum.</li><li>Punt metadata.</li><li>COUNT, AGE and the aged-flow event.</li><li>Duplicates, filling the table to capacity, host TX, and devices without punt headers.</li><li>A concurrent datapath.</li></ul> |
| VPP integration | `pytest vpp/tests` (VPP and passwordless sudo; skipped otherwise) | Kernel TCP/UDP from namespaces on the model's TAP lanes, through VPP and the model. See below. |

The VPP integration tests run VPP with `net_natgw_model,wire=tap`. Each lane's TAP is moved into a namespace: one LAN and two WANs, each WAN with a server behind it. A test decides whether traffic was offloaded from what it can observe. Once a flow is offloaded, VPP's interface counters stop growing while its session counters keep growing, because they are synced back from the hardware.

The tests cover:

- **Offload triggers:** UDP after the ACTIVE threshold, and TCP after its handshake, never before.
- **Removal:** on FIN or RST, on nat44 session delete, on clear, and on disable.
- **Expiry:** an idle session expires and its flows are removed. Traffic seen only by the hardware keeps a session alive.
- **Next hops:** a next-hop MAC change reinstalls the flows.
- **ECMP:** across two WANs the hardware picks the same WAN as VPP, which the replies prove.
- **Table full:** sessions that don't fit stay in software and keep working.
- **Observability:** the CLI and the stats segment gauge.

Fault-injection checks confirm the suites detect real bugs:

- libnatgw: mutations of the table code.
- DPDK: TTL decrement ignored, per-flow age ignored, MACs swapped.
- VPP plugin: ECMP hash ignored, counter refresh skipped, CLOSING ignored, revalidation skipped.

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

### VPP NAT configuration for multiple WANs

This differs from the configuration sketch in the scope of work. With two WANs (ECMP), nat44-ed needs two changes:

1. **Use NAT only as an output feature on each WAN, with no inside interface.** With `set interface nat44 in lan0`, the session is created at LAN input, before the route lookup. Its address therefore ignores the egress WAN. Later packets are also translated before the lookup, so their ECMP hash covers the translated source address and can pick the other WAN, still carrying the first WAN's address.
2. **Add pool addresses with a VRF (`nat44 add address <wan-ip> tenant-vrf 0`), not with `nat44 add interface address`.** An interface-derived address has no VRF, so `nat_ed_alloc_addr_and_port` never matches it to its egress interface. The WAN manager must therefore add and remove these addresses as DHCP leases change.

```
set interface nat44 out wan1 output-feature
set interface nat44 out wan2 output-feature
nat44 add address <wan1 lease> tenant-vrf 0
nat44 add address <wan2 lease> tenant-vrf 0
```
