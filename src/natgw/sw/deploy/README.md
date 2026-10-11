# Deploying natgw on Proxmox VE

This guide takes a Proxmox VE server with an Alveo U200 to a running gateway. The target server is a Dell PowerEdge R7515 (single AMD EPYC). The site has two ISPs:
- **WAN1, AT&T Fiber.** XGS-PON, through a WAS-110 ONT stick that takes the place of AT&T's BGW320 gateway. The stick plugs into the switch through a QSFP28-to-SFP+ adapter.
- **WAN2, Xfinity.** A NETGEAR CM2500 DOCSIS 3.1 cable modem, with its two 1G ports bonded (LACP) to two copper switch ports.

The gateway runs as one VM that owns the U200 through PCI passthrough. Inside the VM run VPP with FPGA offload, the WAN manager, the monitor (a web page of statistics and the ISPs' state), and dnsmasq, which serves the LAN's DHCP and DNS. Other VMs on the host use the gateway like any LAN client.

For the cabling, VLANs and every address, see the [wiring diagram and configuration reference](https://claude.ai/artifact/ND6dAJem8HD2SyUbQYS8a2).

| Directory | Contents |
| --- | --- |
| `host/natgw-host-setup.sh` | Proxmox host preparation (IOMMU, vfio, hugepages, CPU isolation, the pyrite flash tool), a readiness check, and creation of the gateway VM |
| `host/natgw-fpga-hook.sh` | Proxmox hookscript. Before the VM starts, it flashes a staged bitstream and reloads the FPGA |
| `host/natgw-fpga-update` | Stages a bitstream and restarts the VM through Proxmox |
| `guest/natgw-guest-setup.sh` | Builds and installs DPDK, VPP, the WAN manager and the monitor in the VM, and writes their configuration and services |
| `guest/natgw-fpga` | Stages a bitstream on the host and applies it, from inside the VM |
| `switch/nexus-93108tc-fx.conf` | Site switch configuration (Cisco Nexus 93108TC-FX) |
| `BOM.md` | Every part the deployment needs |
| `tests/` | Tests of the host scripts against a fake host (`pytest tests`) |

## What you need

- **Server:** the Proxmox VE node that will hold the U200, running Proxmox VE 8. The recommended VM uses a virtual IOMMU (the `viommu` machine option of current 8.x releases). Without it, see step 5.
- **The card:** an Alveo U200, with the natgw DDR bitstream (`fpga_AU200_nat_ddr`) built from the `natgw-ddr4` branch.
  - The DDR tier uses the DIMM in channel C2. The U200 ships with all four DDR4 DIMMs fitted, so a stock card needs nothing more.
  - The on-chip-only bitstream (`fpga_AU200_nat`) also runs with this branch's software, with the same configuration. Without the DDR tier, the card offloads about 128k sessions instead of 1M.
- **For the first flash:** a machine with Vivado or Vivado Lab Edition and a micro-USB cable to the U200's maintenance port.
- **The switch:** a Cisco Nexus 93108TC-FX (step 3). Also one short QSFP28 passive DAC per used U200 cage.
- **AT&T Fiber (WAN1):**
  - XGS-PON service (step 3 shows how to check).
  - A WAS-110 with an SC/APC connector, and a QSFP28-to-SFP+ adapter (Cisco CVR-QSFP-SFP10G).
  - An SC/APC to SC/APC single-mode patch cable from AT&T's fiber jack to the switch.
  - The BGW320 AT&T installed: its label and status pages hold the values the stick needs. Keep it in service until AT&T closes the installation ticket.
  - A laptop with an Ethernet port, to set up the stick once.
- **Xfinity (WAN2):** a NETGEAR CM2500 cable modem (DOCSIS 3.1, two 1G ports that bond with LACP), activated on the account. Check that it's on Xfinity's approved-device list for your plan.
- **The full parts list:** [`BOM.md`](BOM.md).
- **The ISO:** Ubuntu Server 24.04 LTS, uploaded to the node's ISO storage.

## 1. Server hardware and BIOS (Dell R7515)

**Slot and power.** The U200 is a full-height, ¾-length, dual-slot PCIe Gen3 x16 card.
- It is passively cooled and can draw up to 225 W. Above 75 W it needs its 8-pin auxiliary power cable.
- Confirm with Dell that your R7515 riser configuration has a full-height x16 slot with room for a dual-width card, and that the GPU/accelerator power cable kit for that riser is fitted.
- Prefer a slot whose x16 lanes come straight from the CPU.

**Cooling.** Dell servers set fan speed for cards they don't recognise from the setting *Third-Party PCIe Card Default Cooling Response* (iDRAC: Configuration → System Settings → Hardware Settings → Cooling Configuration).
- Keep it enabled.
- Where iDRAC offers per-slot PCIe airflow (LFM) settings, set the U200's slot to the airflow the U200 data sheet requires.
- Until you've checked the card's temperature under load, run a higher-airflow thermal profile. A passive 225 W card with too little airflow will throttle or shut down.

**BIOS (System Setup).**
- **Virtualization:** Processor Settings → Virtualization Technology **Enabled**, and **IOMMU (AMD-Vi) support enabled**.
- **SR-IOV:** Integrated Devices → **SR-IOV Global Enable** enabled. It helps the platform give PCIe slots separate IOMMU groups.
- **NUMA:** Memory Settings or Processor Settings → **NUMA nodes per socket: NPS1**. This is simplest: one node. With NPS2 or NPS4, pin the VM to the node the U200's slot hangs off (step 4 prints it).
- **System profile:** **Performance**. VPP polls, and C-state exit latency adds jitter.
- **x2APIC:** enabled (the default on EPYC).

## 2. Put the natgw bitstream into the card's flash (once)

The card has to boot the natgw design from its own flash to appear on PCIe as `1234:c001`. A new card ships with a Xilinx image, so the first write needs JTAG. Later updates use `natgw-fpga-update` over PCIe (step 9).

On the build machine, with the U200 installed and powered in the server and its micro-USB port connected:

```
cd taxi/src/cndm/board/Alveo/fpga/fpga_AU200_nat_ddr
make flash        # builds fpga.mcs from fpga.bit and programs the configuration flash over JTAG
```

The build places 256k flows on chip and 4M in DDR (`NAT_DDR_BUCKET_W=20`, 256 MB of the DIMM). It meets timing with little slack (WNS +0.011 ns). If a build misses timing, `NAT_BUCKET_W=14 make flash` (128k on chip) has more slack.

Keep a copy of this bitstream as your known-good (golden) image. Power-cycle the server (a full power cycle, not a warm reboot). Then check on the host that the card enumerates:

```
lspci -nn -d 1234:c001
```

## 3. Switch (Cisco Nexus 93108TC-FX)

`switch/nexus-93108tc-fx.conf` is a complete NX-OS configuration for the site switch: VLANs, the U200 breakout ports, Proxmox, LAN and WAN ports, and management. Review the hostname, NTP servers and mgmt0 address, add your users and AAA, then paste it into the switch's configuration.

| Port | Connects to | VLAN | Notes |
| --- | --- | --- | --- |
| Eth1/49/1 | U200 QSFP0 lane 0 (VPP `CndmEthernet0`) | 10 LAN | 25G, no auto-negotiation, FEC off |
| Eth1/49/2 | U200 QSFP0 lane 1 (`CndmEthernet1`) | 101 WAN1-ATT | 25G, no auto-negotiation, FEC off |
| Eth1/49/3 | U200 QSFP0 lane 2 (`CndmEthernet2`) | 102 WAN2-XFINITY | 25G, no auto-negotiation, FEC off |
| Eth1/49/4, Eth1/50/1–4 | U200 lanes 3–7 | – | shut down |
| Eth1/1 | pve1 (R7515) LAN NIC, bridge `vmbr1` | 10 | |
| Eth1/2 | pve1 cluster NIC (corosync) | 20 PVE-CLUSTER | 10.20.0.0/24, no gateway |
| Eth1/3 | pve1 iDRAC | 10 | |
| Eth1/4–7 | further Proxmox nodes (LAN, cluster) | 10 / 20 | shut until used |
| Eth1/9–39 | LAN clients, access points | 10 | storm control |
| Eth1/40 | the switch's own mgmt0 (192.168.1.4) | 10 | |
| Eth1/51 | AT&T Fiber: the WAS-110, in a QSFP28-to-SFP+ adapter (QSA) | 101 | 10G; no LLDP or CDP, BPDUs filtered |
| Eth1/45 | the WAS-110's setup port (a laptop) | 101 | shut down except while setting up the stick |
| Eth1/46, Eth1/47 | Xfinity CM2500, LAN 1 and LAN 2, as port-channel 46 | 102 | LACP active, 2 × 1G; no LLDP or CDP, BPDUs filtered |
| others | spare | – | shut down |

Things to check:
- **Cabling the card.** Each U200 cage connects to a switch QSFP28 port with one straight QSFP28 passive DAC. Both ends run as 4×25G: `interface breakout module 1 port 49-50 map 25g-4x`.
- **Cable rating.** Taxi's 25G PHY does no auto-negotiation and no FEC, so use a short cable (1–2 m) rated for 25G without FEC (CA-N). Nexus switches may refuse cables not coded for Cisco.
- **Lane order.** At bring-up, check it once: with only Eth1/49/2 enabled, `vppctl show interface` should show link on `CndmEthernet1`.
- **The WAS-110 in the switch.** The stick's host side is 10GBASE-R. The QSA turns QSFP28 port Eth1/51 into an SFP+ port at 10G.
  - Some NX-OS releases set 10G from the adapter and refuse `speed 10000`; if so, leave that line out.
  - NX-OS may log the stick as an unsupported (non-Cisco) transceiver, or refuse to bring the port up. Then put the stick in a 10G SFP+-to-RJ45 media converter instead, and cable the converter to Eth1/45 (unshut it, and use another spare port, put in VLAN 101, for the laptop during setup).
- **Xfinity's link.** The CM2500's two 1G ports form one LACP port-channel (Po46) with Eth1/46 and Eth1/47: up to 2 Gb/s in all, but any one session uses one link, so at most 1 Gb/s. The switch spreads sessions over the links by addresses and ports (`port-channel load-balance src-dst ip-l4port`), since they all share one NAT address. The U200 lane behind it is 25G.
- **The port-channel needs the modem's aggregation on.** It's off by default. With it off, the switch gets no LACP and suspends both ports, so Xfinity stays down rather than risk a loop over the modem's two ports. Turn it on first (below).
- **The switch stays off the WAN.** It has no address in any data VLAN and is managed only through mgmt0, which is cabled to a LAN port. VLAN 102 has the modem's port-channel and the U200 lane. VLAN 101 has the stick and the U200 lane, plus the setup port, which stays shut.

### AT&T Fiber: set up the WAS-110

The WAS-110 is an XGS-PON ONT on an SFP+ module. With the 8311 community firmware and the BGW320's identity, AT&T's network accepts it in place of the BGW320. The procedure follows pon.wiki's [Masquerade as the AT&T BGW320-500/505 with the WAS-110](https://pon.wiki/guides/masquerade-as-the-att-inc-bgw320-500-505-with-the-was-110/). Read it once; it explains each value. This setup isn't supported by AT&T.

1. **Check that the service is XGS-PON.** The BGW320's fiber module has an orange or red bale clasp, or "XGS-PON" or 1270 nm on its label. A green clasp or 1310 nm is GPON, which the WAS-110 can't do. With firmware older than 6.x, `http://192.168.1.254/cgi-bin/fiberstat.ha` also shows a wavelength of 1270 nm.
2. **Write down from the BGW320:**
   - its model, from the label: BGW320-500 or BGW320-505;
   - the ONT ID, from the label or `fiberstat.ha`;
   - its MAC address, from the label;
   - its current software version, from `http://192.168.1.254/cgi-bin/update.ha`.
3. **Reach the stick.** Put the stick in the QSA in Eth1/51, with no fiber in it. On the switch, `interface Ethernet1/45` then `no shutdown`. Cable a laptop to Eth1/45 and give it 192.168.11.3/24, no gateway. The stick answers at `https://192.168.11.1/`. Its web credentials depend on its firmware; see pon.wiki's [WAS-110 page](https://pon.wiki/xgs-pon/ont/bfw-solutions/was-110/#web-credentials).
4. **Install the 8311 community firmware**, on both images, with pon.wiki's [guide](https://pon.wiki/guides/install-the-8311-community-firmware-on-the-was-110/). Resellers' preloaded 8311 builds aren't supported by the community; replace them too.
5. **Set the masquerade values** in `https://192.168.11.1/cgi-bin/luci/admin/8311/config`:

   | Setting | BGW320-500 | BGW320-505 |
   | --- | --- | --- |
   | PON Serial Number | `HUMA` + the ONT ID | `NOKA` + the ONT ID |
   | Equipment ID | `iONT320500X` | `iONT320505X` |
   | Hardware Version | `BGW320-500_2.1` | `BGW320-505_2.2` |
   | Sync Circuit Pack Version | on | on |
   | Software Version A and B | the BGW320's current version (step 2) | the same |
   | MIB File | `/etc/mibs/prx300_1U.ini` | the same |
   | ISP Fixes → Fix VLANs | on | on |
   | Management gateway | `192.168.11.2` | the same |

   Check the table against pon.wiki's current one; AT&T's values change with BGW320 firmware. The management gateway is VPP's address next to the stick (`NATGW_WAN1_ADDRESS`): with it, the stick's page can be reached from the LAN (step 10). Over SSH the same settings are `fwenv_set -8 <name> <value>`, for example `fwenv_set -8 gateway 192.168.11.2`. Save, then reboot the stick.
6. **Put the setup port away.** `shutdown` on Eth1/45 again and unplug the laptop.
7. **Clone the BGW320's MAC.** Set `NATGW_WAN1_MAC` in `deploy.env` (step 7) to the BGW320's MAC. AT&T's DHCP lease follows that MAC, so the gateway gets its address at cut-over without waiting for the old lease to expire.

The fiber moves from the BGW320 to the stick at cut-over (step 8).

### Xfinity: the CM2500 cable modem

The CM2500 serves its LAN 1 port normally. With *Ethernet Port Aggregation* set to LACP, it also bonds LAN 2 with LAN 1 when the other end answers with LACP (IEEE 802.3ad), as the switch does.

1. **Activate it.** Connect it to the coax and power, and cable a laptop to its LAN 1 port. Activate it with the Xfinity app or by phone; Xfinity identifies it by the MAC address and serial number on its label.
2. **Turn on aggregation.** From the laptop, open `http://192.168.100.1/` and log in as `admin` with the password on the modem's label (on some units the default is `password`). Change that password. Then set **ADVANCED → Advanced Setup → Ethernet Port Aggregation** to **Enable (LACP-IEEE 802.3ad)**.
3. **Cable it to the switch.** Unplug the laptop, and remove the plug that covers the LAN 2 port. Cable LAN 1 to Eth1/46 and LAN 2 to Eth1/47.
4. **Check the bond.** On the switch, `show port-channel summary` shows Po46 up, with Eth1/46 and Eth1/47 both bundled (P). The modem's Ethernet Port Aggregation page shows it too. A member shown suspended (s) gets no LACP from the modem: check step 2.
5. **Restart it once more.** After the gateway is up (step 8), power-cycle the modem once. A cable modem serves the first router MAC it sees after starting, and may ignore a new one until it restarts.

The modem's page, `http://192.168.100.1/`, is also reachable from the LAN: `NATGW_WAN2_ROUTES` sends that address out of the Xfinity WAN.

## 4. Prepare the Proxmox host

Use a Linux bridge for the LAN (for example `vmbr1` on the NIC cabled to a VLAN 10 port). Other VMs and the gateway VM's management interface attach to it.
- The host's own management address can be on this bridge (the wiring page uses 192.168.1.3).
- Corosync and other cluster traffic should have their own network that doesn't depend on the gateway.

```
apt-get install -y git
git clone --depth 1 -b natgw-ddr4 https://github.com/gdasher/taxi /root/natgw-taxi
cd /root/natgw-taxi/src/natgw/sw/deploy/host
```

Choose the VM's resources and plan the cores:
- **Pick the cores.** Run `lscpu -e=CPU,NODE,CORE,CACHE` to see the CPU topology. Pick 8 hardware threads on the U200 slot's NUMA node (`natgw-host-setup.sh check`, after the reboot below, prints that node). On EPYC, keep them within one CCD if you can (they share an L3 cache).
- **Leave CPUs 0–1 to the host.**
- **Hugepages:** reserve as many 1 GiB hugepages as the VM has GiB of memory.

```
./natgw-host-setup.sh prepare --hugepages 16 --isolate 4-11
reboot
```

This is what `prepare` does:
- **Kernel command line:** sets `iommu=pt` (AMD's IOMMU is on by default; Intel CPUs also get `intel_iommu=on`), 16 × 1 GiB hugepages, and isolation of the chosen CPUs (`isolcpus`, `nohz_full`, `rcu_nocbs`). It edits GRUB, or `/etc/kernel/cmdline` on systemd-boot installs.
- **vfio:** binds `1234:c001` to `vfio-pci`, loads the vfio modules at boot, and keeps Xilinx XRT drivers off the card.
- **FPGA update tooling:** builds `pyrite` (the PCIe flash tool) into `/usr/local/sbin`, and installs `natgw-fpga-update` and the hook script (`/var/lib/vz/snippets/natgw-fpga-hook.sh`).
- **Configuration:** writes `/etc/natgw/fpga.conf` with the card's address, and enables *snippets* on the `local` storage.

After the reboot:

```
./natgw-host-setup.sh check
```

It must print `OK`:
- The IOMMU is on.
- The card is alone in its IOMMU group. If it isn't, try another slot. Avoid `pcie_acs_override`, which weakens isolation.
- The card is bound to `vfio-pci`.
- The hugepages are reserved and pyrite and the hook are installed.

It also prints the card's PCIe link (expect 8 GT/s, x16) and its NUMA node.

## 5. Create the gateway VM

```
./natgw-host-setup.sh create-vm --vmid 100 --storage local-lvm --bridge vmbr1 \
    --iso local:iso/ubuntu-24.04.3-live-server-amd64.iso \
    --cores 8 --memory 16384 --affinity 4-11
```

| Setting | Why |
| --- | --- |
| `machine: q35,viommu=intel`, OVMF | PCIe passthrough, and a virtual IOMMU so DPDK can use `vfio-pci` safely in the VM. The emulated IOMMU is Intel-type even on an AMD host. |
| `cpu: host`, `numa: 1`, `affinity` | Full CPU features for DPDK and VPP, on the cores isolated in step 4 |
| `memory`, `balloon: 0`, `hugepages: 1024` | Guest memory backed by 1 GiB host pages. Passthrough pins it anyway, so ballooning is off. |
| `hostpci0: <card>,pcie=1` | The U200, as a PCIe device |
| `net0: virtio`, `52:54:00:4E:47:02`, LAN bridge | The VM's management interface on the LAN (192.168.1.2) |
| `onboot: 1`, `startup: order=1` | Starts first: the LAN's DHCP and DNS run in it |
| `hookscript` | `natgw-fpga-hook.sh`, the FPGA update path (step 9) |

The VM can only run on this node, so don't add it to HA (or use an HA group restricted to this node).

If your Proxmox version rejects `viommu`:
1. Create the VM with `machine: q35` instead (edit the script's `--machine`, or `qm set 100 --machine q35`).
2. Set `NATGW_IOMMU_MODE=noiommu` in the guest's `deploy.env` (step 7). DPDK then uses vfio's no-IOMMU mode. That works, but it gives up DMA isolation inside the VM.

## 6. Install Ubuntu in the VM

Start the VM and open its console in the Proxmox web UI. Install Ubuntu Server 24.04:
- minimal or standard;
- OpenSSH server;
- no snaps;
- the whole disk.

The VM needs internet access while it's being built. If the old router still serves the LAN, accept its DHCP lease on `enp1s0` during the install. The gateway's static address is applied later, at cut-over (step 8).

After the installer finishes, remove the ISO (`qm set 100 --ide2 none,media=cdrom`) and reboot the VM.

## 7. Set up the gateway VM

In the VM, as root:

```
apt-get install -y git
git clone --depth 1 -b natgw-ddr4 https://github.com/gdasher/taxi /opt/natgw/src/taxi
/opt/natgw/src/taxi/src/natgw/sw/deploy/guest/natgw-guest-setup.sh
```

The first run writes `/etc/natgw/deploy.env` and stops. Edit it:
- LAN addresses and DHCP range;
- DNS upstreams;
- the CPU VPP polls on (a vCPU other than 0);
- the NAT session limit, 1M by default (see *Flow tables and the DDR tier* in step 10);
- the WANs: names (`att` and `xfinity`, used in alerts and on the monitor page), the BGW320's MAC for AT&T (`NATGW_WAN1_MAC`, step 3), VPP's address next to the WAS-110 (`NATGW_WAN1_ADDRESS`, 192.168.11.2/24), and the route to the cable modem's page (`NATGW_WAN2_ROUTES`);
- where the monitor's page is served (`NATGW_MONITOR_BIND`, `NATGW_MONITOR_PORT`; see step 11);
- the Proxmox host's address and this VM's ID, used for FPGA updates.

Then run the script again. It takes 30–60 minutes, mostly building VPP:

```
/opt/natgw/src/taxi/src/natgw/sw/deploy/guest/natgw-guest-setup.sh
reboot
```

What the script does:
1. **Packages:** installs build tools.
2. **Sources:** clones `taxi` (branch `natgw-ddr4`, `NATGW_BRANCH`), and `dpdk` and `vpp` (branch `natgw`) into `/opt/natgw/src`.
3. **DPDK:** builds and installs it to `/usr/local`.
   - Only the drivers the gateway uses are included: the PCI, vdev and VMBus buses (VPP needs all three), ring and stack mempools, `common_natgw`, `net_cndm` and `net_natgw_model`.
   - It's built for generic x86-64 (`platform=generic`), never for the build machine's CPU, so the same build runs on any server.
4. **VPP:** builds it with the nat44-ed hooks and the `natgw_offload` plugin into `/opt/natgw/vpp`.
5. **WAN manager and monitor:** installs them into `/opt/natgw/wanmgr` and `/opt/natgw/monitor`, with a virtualenv holding `vpp_papi`.
6. **Configuration:** writes it from templates:
   - `/etc/vpp/startup.conf`, with the card's PCI address detected and any `NATGW_CARD_DEVARGS`. The device's counters reach the stats segment every second.
     - VPP's main heap is 2 GB (`NATGW_VPP_HEAP`). nat44-ed preallocates its sessions there: 430 MB for 1M.
     - The offload plugin's sync budget follows the session limit (limit ÷ 64, at least 4096), so each session is synced at least every 64 s.
   - `/etc/vpp/natgw.cli`, including each WAN's cloned MAC, extra address and routes.
   - `/etc/natgw/wanmgr.toml`, only if missing, with the WANs' names. To rename a WAN later, edit `name` there and restart `natgw-wanmgr`.
   - `/etc/natgw/monitor.toml`, regenerated from `deploy.env` on every run.
   - The monitor's HTTPS certificate: yours, or a self-signed one created once in `/etc/natgw/tls`.
   - `/etc/dnsmasq.d/natgw-lan.conf` and `/etc/natgw/60-natgw-mgmt.yaml`.
7. **Kernel and devices:**
   - vfio for the card (`natgw-card-check` also rebinds it before VPP starts);
   - 2 MiB hugepages for VPP;
   - `intel_iommu=on iommu=pt` on the kernel command line for the virtual IOMMU.
8. **Services:** installs and enables `vpp.service`, `natgw-wanmgr.service` and `natgw-monitor.service`. The monitor runs as the unprivileged user `natgw-mon`, which is in the `vpp` group so it can read the stats socket.

The script can be run again at any time: it updates and rebuilds, and keeps an edited `wanmgr.toml`.

Add your SMTP relay to `/etc/natgw/wanmgr.toml` for email alerts, and set probe targets that each WAN can reach. Then check:

```
systemctl status vpp natgw-wanmgr
vppctl show interface                     # CndmEthernet0..7, LAN, WAN1 and WAN2 up
vppctl show hardware-interfaces CndmEthernet0 detail | grep natgw_   # shim counters
vppctl show natgw offload
vppctl show hardware-interfaces CndmEthernet0 detail | grep -E 'natgw_(ddr_status|onchip_capacity|ddr_capacity)'
                                          # 23 (present, calibrated, enabled, active), 262144, 4194304
cat /run/natgw-wanmgr.json                # WAN states and leases
systemctl status natgw-monitor            # then open https://192.168.1.2:8443/ (step 11)
```

## 8. Cut-over

1. Connect the ISPs:
   - **AT&T:** move the fiber from the BGW320 to the WAS-110 in Eth1/51. Clean both SC/APC connectors first, and push until it clicks. Don't look into the fiber.
   - **Xfinity:** the CM2500's LAN 1 and LAN 2 are on Eth1/46 and Eth1/47, with Po46 up (step 3).
2. Take the old router off the LAN, or at least turn off its DHCP server.
3. In the VM, run:
   ```
   natgw-guest-setup.sh --no-build --apply-network
   ```
   This gives `enp1s0` its static address (192.168.1.2, gateway 192.168.1.1, which is VPP) and starts dnsmasq's DHCP and DNS on the LAN.
4. Check the result:
   - Clients renew to 192.168.1.100–199, with gateway 192.168.1.1 and DNS 192.168.1.2.
   - `/run/natgw-wanmgr.json` shows both WANs up with leases.
   - `vppctl show ip fib 0.0.0.0/0` shows the API-sourced route with both gateways.
   - The monitor page shows both ISPs up and no alerts.
   - The WAS-110's page, `https://192.168.11.1/cgi-bin/luci/admin/status/overview`, shows PON PLOAM status "O5.1, Associated" and optical levels within specification. Its VLAN page (`/cgi-bin/luci/admin/8311/vlans`) lists extended VLAN tables; "No Extended VLAN Tables Detected" means AT&T's network doesn't accept the masquerade values. pon.wiki's [troubleshooting guide](https://pon.wiki/guides/troubleshoot-connectivity-issues-with-the-was-110-or-x-onu-sfpp/) covers both.
   - Power-cycle the Xfinity modem once (step 3).
5. Test failover: unplug the Xfinity modem's coax, or the AT&T fiber from the WAS-110. Within about 6 s:
   - that WAN goes down and the route keeps only the other WAN;
   - an alert is mailed;
   - the monitor shows the ISP down with the reason (no reply from its probe targets), and its outage starts.
6. Plug it back in. About 10 s later, the WAN is back up, and the outage appears in the monitor's history with its duration.

## 9. Updating the FPGA

A new bitstream is written to the card's flash and loaded when the VM next starts.
- **Why the reload runs on the host:** reloading the FPGA drops the PCIe link, so it runs on the host while the VM is stopped (`natgw-fpga-hook.sh`, Proxmox phase `pre-start`).
- **Why not reboot from inside the guest:** that keeps the same QEMU process and doesn't run the hook. Use `natgw-fpga-update --restart` or `qm shutdown` followed by `qm start`.

On the host:

```
natgw-fpga-update --stage fpga.bit        # staged in /var/lib/natgw-fpga/pending
natgw-fpga-update --restart 100           # shutdown; the hook flashes, verifies and reloads; start
natgw-fpga-update --status
journalctl -t natgw-fpga                   # what the hook did; pyrite's output: /var/lib/natgw-fpga/flash.log
```

Or from inside the VM. This needs root SSH access from the VM to the host, for example `ssh-copy-id root@192.168.1.3`:

```
natgw-fpga stage fpga.bit
natgw-fpga apply                          # the host restarts this VM
```

What the hook does at `pre-start`:
1. If an image is staged, it writes it to the flash with `pyrite -w`. Pyrite verifies the write. The image then becomes `/var/lib/natgw-fpga/current.bit`, with a copy kept in `history/`.
2. It reloads the FPGA from flash (`pyrite -b`). This also hot-resets the PCIe port and rescans it.
3. It checks the card came back as `1234:c001` and is bound to `vfio-pci`.

If something goes wrong:
- **A failed write** leaves the old image in place. The staged file is renamed `*.failed` and isn't retried.
- **Errors** are logged. The VM still starts unless `NATGW_STRICT=yes` is set in `/etc/natgw/fpga.conf`.
- **Reloading at every start:** set `NATGW_RELOAD_ON_START=yes` to fully reset the card at each start (about 10 s extra).

**Moving a gateway from the `natgw` branch to the DDR tier.** In the VM, switch the taxi checkout first: the setup script from the `natgw` branch would ask for `natgw-ddr4` in the DPDK and VPP repositories too.

```
git -C /opt/natgw/src/taxi fetch origin +refs/heads/natgw-ddr4:refs/remotes/origin/natgw-ddr4
git -C /opt/natgw/src/taxi checkout -B natgw-ddr4 origin/natgw-ddr4
```

In `/etc/natgw/deploy.env`, set `NATGW_BRANCH=natgw-ddr4` and `NATGW_NAT_SESSIONS=1048576`. The other new settings take their defaults when missing. Run `natgw-guest-setup.sh`, then stage the `fpga_AU200_nat_ddr` bitstream and apply it as above. The new software also runs on the old bitstream, so the order doesn't matter.

Expect about a minute of gateway outage per update. If a new image doesn't enumerate at all, write the golden image back over JTAG (step 2).

## 10. Operating notes

- **Maintenance.** The VM can't migrate, so maintenance or a reboot of this node is an internet outage for the site.
- **Host updates.** Proxmox host updates download through the gateway. Plan them while the gateway is up.
- **Backups.** Use `snapshot` mode (disk only). Snapshots that include RAM, and suspend, aren't possible with passthrough, and a `stop`-mode backup stops the gateway.
- **Proxmox firewall.** If you enable it on the gateway VM's LAN interface, allow the VM to act as a DHCP server. The per-VM DHCP option is aimed at clients, and the IP/MAC filters can block a DHCP server.
- **CPU load.** VPP polls, so one of the VM's cores shows 100% all the time. That's expected; don't overcommit the pinned cores.
- **Worker threads.** VPP runs on its main thread only. Worker threads haven't been tested with the offload path.

### ISP equipment

- **The WAS-110's page** is `https://192.168.11.1/` from any LAN client. It shows the PON state and optical levels, and holds the masquerade settings. AT&T's network can turn the page off; then it's reached only with the fiber unplugged, from the setup port (step 3).
- **AT&T emails** saying "Check your AT&T Fiber equipment" are expected: the BGW320 sent telemetry and the stick doesn't.
- **Updating the stick.** Follow the 8311 project's release notes. Write down the masquerade values first.
- **The cable modem's page** is `http://192.168.100.1/` from any LAN client. Its Ethernet Port Aggregation page shows the bond's state. A factory reset turns aggregation off, which takes Xfinity down until it's turned on again (step 3, from a laptop on LAN 1).
- **Xfinity's link.** If one of the two cables fails, the port-channel carries on at 1 Gb/s. `show port-channel summary` shows it.
- **Sharing traffic between the ISPs.** New sessions are spread across the healthy WANs by ECMP, equally by default. To match the plans' speeds, set `weight` in each `[[wan]]` block of `/etc/natgw/wanmgr.toml`, for example 5 for a 5 Gb/s AT&T plan against 2 for Xfinity's 2 Gb/s, then restart `natgw-wanmgr`.

### Flow tables and the DDR tier

Each NAT session is two flows, one per direction. The DDR bitstream holds 256k flows on chip and 4M in DDR.
- **Placement.** New flows go on chip until it's 85% full, then to DDR. Idle on-chip flows move to DDR, and busy DDR flows move on chip. Moves don't lose packets. DDR flows have no hardware packet counters, but their activity still keeps their sessions alive.
- **The session limit.** `NATGW_NAT_SESSIONS` is 1M by default (2M flows). That leaves room to spare in the tables, whose placement can't fill them completely. Above about 2M sessions, not every session can be offloaded: the rest stay in software, which is slower but still works. Each 1M sessions needs about 430 MB of `NATGW_VPP_HEAP`.
- **When the tier isn't working.** If the DIMM doesn't calibrate, the gateway runs on the on-chip table only (about 128k offloaded sessions) and the monitor raises *DDR present but not calibrated*. `vppctl show hardware-interfaces CndmEthernet0 detail` shows `natgw_ddr_status`: 23 is working, 1 means the memory didn't calibrate, and 3 means the tier is turned off.
- **Turning it off.** Set `NATGW_CARD_DEVARGS=natgw_ddr=0` and run `natgw-guest-setup.sh --no-build`, then restart VPP.
- **Tuning.** `NATGW_CARD_DEVARGS` also takes the tier policy's options (`tier_policy=fill`, `onchip_high`, `onchip_low`, `demote_idle`, `promote_k`, `promote_n`, `migrate_budget`, `min_residency`), separated by commas. They're described under *DDR tier* in `sw/README.md`. The defaults are the tested ones.
- **Bulk transfers.** `NATGW_BULK_UDP_PORTS` lists UDP server ports whose sessions go straight to DDR and stay there, which keeps the on-chip table for other traffic. None by default.
- **Watching it.** The monitor graphs on-chip and DDR table fill, tier moves and the DDR hit rate. It alerts on DDR read errors, failed tier moves, and the on-chip table above 85% for a minute.

## 11. Monitoring

The monitor serves a page with the gateway's statistics and the ISPs' state at `https://<NATGW_MONITOR_BIND>:<NATGW_MONITOR_PORT>/`, by default `https://192.168.1.2:8443/`. It only reads VPP's statistics and the WAN manager's status, so it can't affect forwarding.

**What it shows:**
- **ISPs.** Each WAN's state, for how long, and why. The reason is one of: no reply from its probe targets, link down, administratively down, no DHCP lease, or interface missing. The page also shows the lease, link, whether it's in the default route, inbound and outbound traffic, the last 60 probe rounds and each target's last reply. The headline says when an ISP is down, and the signal path at the top draws its link cut where the fault is.
- **Outage history.** Start, end, duration, reason, and the evidence when it started: probe results, lease, link, the other ISPs, and the traffic just before.
- **Alerts:**
  - an ISP down, critical when no ISP is working;
  - VPP not running;
  - the WAN manager not reporting;
  - a port's link down;
  - offload disabled;
  - DDR present but not calibrated;
  - the on-chip table above 85% for a minute;
  - NAT sessions near the maximum;
  - any rise in: frames with bad FCS, DDR read errors, lost device events, failed tier moves, table-full installs, install queue overflow, given-up installs, rte_flow errors.
- **Right now** figures:
  - packets forwarded by the FPGA, sent to VPP, and dropped;
  - on-chip and DDR table fill;
  - offloaded and NAT sessions;
  - VPP.
- **Graphs** over 10 minutes, an hour, a day, a week, 30 days or a year. Each has a table view.
- **The ports, the event log, and every counter.** Any counter can be graphed over the year.
- **The same data as JSON:**
  - `/api/stats`;
  - `/api/history?seconds=600` (up to a year);
  - `/api/counter?name=...`;
  - `/api/events?limit=200`;
  - `/healthz`, for an external check.

**Where it listens.** `NATGW_MONITOR_BIND` takes addresses and interface names, separated by spaces. An interface name serves on each address the interface has, and waits for one if it has none yet. The default is the management interface (`enp1s0`).
- There is no login: anyone who can reach the address can read the page.
- To restrict it, set `NATGW_MONITOR_BIND=127.0.0.1` and use an SSH tunnel: `ssh -L 8443:127.0.0.1:8443 root@192.168.1.2`, then open `https://localhost:8443/`.
- If the Proxmox firewall is on for the VM, allow TCP 8443 from the LAN.
- For port 443, set `NATGW_MONITOR_PORT=443`. The service holds only the capability to bind low ports.

**HTTPS.** HTTPS is the only protocol served; TLS 1.2 is the minimum.
- **Default certificate.** Without a certificate of your own, the setup script creates a self-signed one for the VM's name and management address. It prints the SHA-256 fingerprint so you can check it when the browser asks.
- **Your own certificate.** Set `NATGW_MONITOR_CERT` and `NATGW_MONITOR_KEY` to PEM files that the `natgw-mon` user can read, then run the script with `--no-build`.
- **Renewals.** After a renewal, `systemctl reload natgw-monitor` loads the new files without a restart.
- **Plain HTTP.** `NATGW_MONITOR_TLS=no` turns HTTPS off. That's only sensible on 127.0.0.1.

**Changing settings.** Edit `NATGW_MONITOR_*` in `/etc/natgw/deploy.env` and run `natgw-guest-setup.sh --no-build`. That rewrites `/etc/natgw/monitor.toml` and restarts the monitor. Alert thresholds are in that file, but the script overwrites it.

**Troubleshooting:**
- `journalctl -u natgw-monitor` shows the monitor's log.
- `kill -USR1 $(systemctl show -p MainPID --value natgw-monitor)` writes every thread's stack to the journal.
- **Statistics** are in `/var/lib/natgw-monitor/stats.duckdb` (DuckDB):
  - one-second samples for 30 days;
  - minute summaries and a snapshot of every counter each minute, for a year.

  That's about 410 MB once a year has accumulated; plan for 1 GB of disk.
- **Reading them.** DuckDB locks the file to the monitor while it runs, so read statistics through the API (`/api/history`, `/api/counter`).
- **Events and outages** are in `monitor.db` (SQLite), kept for a year. You can read them while the monitor runs, for example `sqlite3 /var/lib/natgw-monitor/monitor.db "SELECT datetime(start, 'unixepoch'), wan, end - start, reason FROM outages"`.
- **Retention** is set in `[store]` of `monitor.toml`.
- **Memory.** The monitor uses up to about 400 MB, mostly DuckDB's cache (`memory_mb`, 256). The service is limited to 1 GB.

## What has been tested

- **Host scripts:** `tests/` runs the hook and `natgw-fpga-update` against a fake host. It covers update, failure, strict mode, wrong card ID, rebinding and restart.
- **Guest setup script:** run end to end with `--skip-hw` in a fresh Ubuntu 24.04 system container (systemd-nspawn), from a bare install through packages, sources, DPDK, VPP, the WAN manager, configuration and services. Then checked:
  - the rendered configuration, with dnsmasq staged but disabled until cut-over;
  - the systemd units, which validate with `systemd-analyze verify`;
  - DPDK built for generic x86-64 (`-march=corei7`) with exactly the intended drivers;
  - all plugin libraries resolve;
  - the installed VPP starts with the FPGA model device (`net_natgw_model`), creates `NatgwModel0–2`, and enables the natgw offload and nat44.
- **Monitor deployment:** in a fresh Ubuntu 24.04 container booted with systemd, with the virtualenv, WAN manager and monitor pre-installed instead of built, `natgw-guest-setup.sh --skip-hw --no-build` was run, and then:
  - the `natgw-mon` user, the self-signed certificate and its permissions, and the rendered `monitor.toml` were checked;
  - the hardened `natgw-monitor.service` started and served HTTPS on the configured address, reporting VPP down (there was no VPP);
  - binding to an interface name and supplying your own certificate were checked.

  The monitor itself is tested against real VPP and the WAN manager by `sw/monitor/tests`.
- **ISP settings:**
  - `natgw.cli`, `startup.conf` and `wanmgr.toml` are rendered by the script's own code from `deploy.env.example` and overrides (`tests/test_guest_config.py`). That includes the WAN names, the cloned MAC, the extra address and routes, and refusing malformed values.
  - On the simulated site (`sw/wanmgr/tests/test_wan_devices.py`):
    - VPP, with these settings and the WAN manager, gets its DHCP lease with the cloned MAC.
    - LAN clients reach a stand-in for the stick's page (192.168.11.1, management gateway 192.168.11.2) and for the modem's page (192.168.100.1).
    - Internet traffic still uses only the leases, and is offloaded.
    - The stick's page stays reachable while its WAN is down, and failover and recovery are unchanged.
- **DDR defaults:** `startup.conf` rendered with and without devargs and bulk ports. VPP started with the 2 GB heap, the derived sync budget and bulk ports, and the 1M-session NAT limit (432 MB used of the heap). The DDR tier itself is tested on the C model and the RTL (`sw/README.md`).
- **Not tested (no hardware or Proxmox here):** the hardware steps (vfio, hugepages, kernel command line), `natgw-host-setup.sh` on a real Proxmox host, Pyrite flashing of a U200, the VM settings, DDR calibration on a real card, the sync budget's main-thread cost on the card, the WAS-110 and the QSA in the Nexus, AT&T's acceptance of the masquerade, and the CM2500 and its LACP bond with the Nexus.
