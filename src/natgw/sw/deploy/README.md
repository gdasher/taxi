# Deploying natgw on Proxmox VE

This guide takes a Proxmox VE server with an Alveo U200 to a running gateway. The target server is a Dell PowerEdge R7515 (single AMD EPYC).

The gateway runs as one VM that owns the U200 through PCI passthrough. Inside the VM run VPP with FPGA offload, the WAN manager, and dnsmasq, which serves the LAN's DHCP and DNS. Other VMs on the host use the gateway like any LAN client.

For the cabling, VLANs and every address, see the [wiring diagram and configuration reference](https://claude.ai/artifact/ND6dAJem8HD2SyUbQYS8a2).

| Directory | Contents |
| --- | --- |
| `host/natgw-host-setup.sh` | Proxmox host preparation (IOMMU, vfio, hugepages, CPU isolation, the pyrite flash tool), a readiness check, and creation of the gateway VM |
| `host/natgw-fpga-hook.sh` | Proxmox hookscript. Before the VM starts, it flashes a staged bitstream and reloads the FPGA |
| `host/natgw-fpga-update` | Stages a bitstream and restarts the VM through Proxmox |
| `guest/natgw-guest-setup.sh` | Builds and installs DPDK, VPP and the WAN manager in the VM, and writes their configuration and services |
| `guest/natgw-fpga` | Stages a bitstream on the host and applies it, from inside the VM |
| `switch/nexus-93108tc-fx.conf` | Site switch configuration (Cisco Nexus 93108TC-FX) |
| `tests/` | Tests of the host scripts against a fake host (`pytest tests`) |

## What you need

- **Server:** the Proxmox VE node that will hold the U200, running Proxmox VE 8. The recommended VM uses a virtual IOMMU (the `viommu` machine option of current 8.x releases). Without it, see step 5.
- **The card:** an Alveo U200, with the natgw bitstream (`fpga_AU200_nat`) built from the `natgw` branch.
- **For the first flash:** a machine with Vivado or Vivado Lab Edition and a micro-USB cable to the U200's maintenance port.
- **The switch:** a Cisco Nexus 93108TC-FX (step 3). Also one short QSFP28 passive DAC per used U200 cage.
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
cd taxi/src/cndm/board/Alveo/fpga/fpga_AU200_nat
make flash        # builds fpga.mcs from fpga.bit and programs the configuration flash over JTAG
```

Keep a copy of this bitstream as your known-good (golden) image. Power-cycle the server (a full power cycle, not a warm reboot). Then check on the host that the card enumerates:

```
lspci -nn -d 1234:c001
```

## 3. Switch (Cisco Nexus 93108TC-FX)

`switch/nexus-93108tc-fx.conf` is a complete NX-OS configuration for the site switch: VLANs, the U200 breakout ports, Proxmox, LAN and WAN ports, and management. Review the hostname, NTP servers and mgmt0 address, add your users and AAA, then paste it into the switch's configuration.

| Port | Connects to | VLAN | Notes |
| --- | --- | --- | --- |
| Eth1/49/1 | U200 QSFP0 lane 0 (VPP `CndmEthernet0`) | 10 LAN | 25G, no auto-negotiation, FEC off |
| Eth1/49/2 | U200 QSFP0 lane 1 (`CndmEthernet1`) | 101 WAN1 | 25G, no auto-negotiation, FEC off |
| Eth1/49/3 | U200 QSFP0 lane 2 (`CndmEthernet2`) | 102 WAN2 | 25G, no auto-negotiation, FEC off |
| Eth1/49/4, Eth1/50/1–4 | U200 lanes 3–7 | – | shut down |
| Eth1/1 | pve1 (R7515) LAN NIC, bridge `vmbr1` | 10 | |
| Eth1/2 | pve1 cluster NIC (corosync) | 20 PVE-CLUSTER | 10.20.0.0/24, no gateway |
| Eth1/3 | pve1 iDRAC | 10 | |
| Eth1/4–7 | further Proxmox nodes (LAN, cluster) | 10 / 20 | shut until used |
| Eth1/9–39 | LAN clients, access points | 10 | storm control |
| Eth1/40 | the switch's own mgmt0 (192.168.1.4) | 10 | |
| Eth1/45 | ISP 1 modem | 101 | no LLDP or CDP, BPDUs filtered |
| Eth1/46 | ISP 2 modem | 102 | no LLDP or CDP, BPDUs filtered |
| others | spare | – | shut down |

Things to check:
- **Cabling the card.** Each U200 cage connects to a switch QSFP28 port with one straight QSFP28 passive DAC. Both ends run as 4×25G: `interface breakout module 1 port 49-50 map 25g-4x`.
- **Cable rating.** Taxi's 25G PHY does no auto-negotiation and no FEC, so use a short cable (1–2 m) rated for 25G without FEC (CA-N). Nexus switches may refuse cables not coded for Cisco.
- **Lane order.** At bring-up, check it once: with only Eth1/49/2 enabled, `vppctl show interface` should show link on `CndmEthernet1`.
- **Modem link speed.** The 93108TC-FX's copper ports run at 100M, 1G or 10G. A modem whose fastest port is 2.5G or 5G links at 1G, so for service above 1 Gb/s use a modem port that does 10G.
- **The switch stays off the WAN.** It has no address in any data VLAN and is managed only through mgmt0, which is cabled to a LAN port. WAN VLANs 101 and 102 each contain exactly two ports: the modem and the U200 lane.

## 4. Prepare the Proxmox host

Use a Linux bridge for the LAN (for example `vmbr1` on the NIC cabled to a VLAN 10 port). Other VMs and the gateway VM's management interface attach to it.
- The host's own management address can be on this bridge (the wiring page uses 192.168.1.3).
- Corosync and other cluster traffic should have their own network that doesn't depend on the gateway.

```
apt-get install -y git
git clone --depth 1 -b natgw https://github.com/gdasher/taxi /root/natgw-taxi
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
git clone --depth 1 -b natgw https://github.com/gdasher/taxi /opt/natgw/src/taxi
/opt/natgw/src/taxi/src/natgw/sw/deploy/guest/natgw-guest-setup.sh
```

The first run writes `/etc/natgw/deploy.env` and stops. Edit it:
- LAN addresses and DHCP range;
- DNS upstreams;
- the CPU VPP polls on (a vCPU other than 0);
- the Proxmox host's address and this VM's ID, used for FPGA updates.

Then run the script again. It takes 30–60 minutes, mostly building VPP:

```
/opt/natgw/src/taxi/src/natgw/sw/deploy/guest/natgw-guest-setup.sh
reboot
```

What the script does:
1. **Packages:** installs build tools.
2. **Sources:** clones `taxi`, `dpdk` and `vpp` (branch `natgw`) into `/opt/natgw/src`.
3. **DPDK:** builds and installs it to `/usr/local`.
   - Only the drivers the gateway uses are included: the PCI, vdev and VMBus buses (VPP needs all three), ring and stack mempools, `common_natgw`, `net_cndm` and `net_natgw_model`.
   - It's built for generic x86-64 (`platform=generic`), never for the build machine's CPU, so the same build runs on any server.
4. **VPP:** builds it with the nat44-ed hooks and the `natgw_offload` plugin into `/opt/natgw/vpp`.
5. **WAN manager:** installs it into `/opt/natgw/wanmgr`, with a virtualenv holding `vpp_papi`.
6. **Configuration:** writes it from templates: `/etc/vpp/startup.conf` (with the card's PCI address detected), `/etc/vpp/natgw.cli`, `/etc/natgw/wanmgr.toml` (only if missing), `/etc/dnsmasq.d/natgw-lan.conf` and `/etc/natgw/60-natgw-mgmt.yaml`.
7. **Kernel and devices:**
   - vfio for the card (`natgw-card-check` also rebinds it before VPP starts);
   - 2 MiB hugepages for VPP;
   - `intel_iommu=on iommu=pt` on the kernel command line for the virtual IOMMU.
8. **Services:** installs and enables `vpp.service` and `natgw-wanmgr.service`.

The script can be run again at any time: it updates and rebuilds, and keeps an edited `wanmgr.toml`.

Add your SMTP relay to `/etc/natgw/wanmgr.toml` for email alerts, and set probe targets that each WAN can reach. Then check:

```
systemctl status vpp natgw-wanmgr
vppctl show interface                     # CndmEthernet0..7, LAN, WAN1 and WAN2 up
vppctl show hardware-interfaces CndmEthernet0 detail | grep natgw_   # shim counters
vppctl show natgw offload
cat /run/natgw-wanmgr.json                # WAN states and leases
```

## 8. Cut-over

1. Connect the ISP modems to their VLAN 101 and 102 ports.
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
5. Test failover: unplug one modem's upstream link. Within about 6 s that WAN goes down, the route keeps only the other WAN, and an alert is mailed.
6. Plug it back in. About 10 s later, the WAN is back up.

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

Expect about a minute of gateway outage per update. If a new image doesn't enumerate at all, write the golden image back over JTAG (step 2).

## 10. Operating notes

- **Maintenance.** The VM can't migrate, so maintenance or a reboot of this node is an internet outage for the site.
- **Host updates.** Proxmox host updates download through the gateway. Plan them while the gateway is up.
- **Backups.** Use `snapshot` mode (disk only). Snapshots that include RAM, and suspend, aren't possible with passthrough, and a `stop`-mode backup stops the gateway.
- **Proxmox firewall.** If you enable it on the gateway VM's LAN interface, allow the VM to act as a DHCP server. The per-VM DHCP option is aimed at clients, and the IP/MAC filters can block a DHCP server.
- **CPU load.** VPP polls, so one of the VM's cores shows 100% all the time. That's expected; don't overcommit the pinned cores.
- **Worker threads.** VPP runs on its main thread only. Worker threads haven't been tested with the offload path.

## What has been tested

- **Host scripts:** `tests/` runs the hook and `natgw-fpga-update` against a fake host. It covers update, failure, strict mode, wrong card ID, rebinding and restart.
- **Guest setup script:** run end to end with `--skip-hw` in a fresh Ubuntu 24.04 system container (systemd-nspawn), from a bare install through packages, sources, DPDK, VPP, the WAN manager, configuration and services. Then checked:
  - the rendered configuration, with dnsmasq staged but disabled until cut-over;
  - the systemd units, which validate with `systemd-analyze verify`;
  - DPDK built for generic x86-64 (`-march=corei7`) with exactly the intended drivers;
  - all plugin libraries resolve;
  - the installed VPP starts with the FPGA model device (`net_natgw_model`), creates `NatgwModel0–2`, and enables the natgw offload and nat44.
- **Not tested (no hardware or Proxmox here):** the hardware steps (vfio, hugepages, kernel command line), `natgw-host-setup.sh` on a real Proxmox host, Pyrite flashing of a U200, and the VM settings.
