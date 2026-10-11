# natgw site: bill of materials

These are the parts for one gateway site, as deployed by [`README.md`](README.md):
- a Proxmox VE server with an Alveo U200;
- a Cisco Nexus 93108TC-FX switch;
- AT&T Fiber through a WAS-110 ONT stick (WAN1);
- Xfinity through a NETGEAR CM2500 cable modem, bonded to the switch over two 1G links (WAN2).

Part numbers are the ones the guide and the switch configuration assume. "Or equivalent" means any part with the same function and ratings. Prices aren't listed.

## Server and FPGA card

| # | Item | Part / example | Qty | Notes |
| --- | --- | --- | --- | --- |
| 1 | Server | Dell PowerEdge R7515, single AMD EPYC | 1 | See the configuration rows below; README step 1 |
| 1a | — riser | a riser with a full-height, full-length x16 slot with room for a dual-width card | 1 | Prefer a slot wired straight to the CPU |
| 1b | — accelerator power | Dell's GPU/accelerator enablement kit for that riser (8-pin auxiliary power cable) | 1 | The U200 needs it above 75 W |
| 1c | — power supplies | redundant pair, sized by Dell's configurator for a 225 W accelerator | 2 | |
| 1d | — memory | 64 GB or more of RDIMM | — | The gateway VM takes 16 GiB of 1 GiB hugepages; the rest is the host and other VMs |
| 1e | — boot storage | mirrored SSDs (for example Dell BOSS) | 1 set | Proxmox VE, the VM disk, the monitor's 1 GB history |
| 1f | — network | the onboard 1 GbE ports | 2 | LAN bridge `vmbr1` (Eth1/1) and cluster network (Eth1/2) |
| 1g | — iDRAC | iDRAC9, with its dedicated port | 1 | Eth1/3 |
| 2 | FPGA card | AMD Alveo U200, passive: A-U200-P64G-PQ-G | 1 | Ships with four 16 GB DDR4 RDIMMs; the DDR tier uses the one in channel C2 |
| 3 | JTAG cable | USB-A to micro-USB (data) | 1 | First flash of the bitstream (README step 2) |

## Switch and cabling

| # | Item | Part / example | Qty | Notes |
| --- | --- | --- | --- | --- |
| 4 | Switch | Cisco Nexus N9K-C93108TC-FX | 1 | 48 × 100M/1G/10GBASE-T, 6 × 40/100G QSFP28 |
| 4a | — power supplies | a matching pair, port-side intake or exhaust to suit the rack (for example NXA-PAC-650W-PI or -PE) | 2 | |
| 4b | — fans | modules with the same airflow direction as the PSUs | 4 | |
| 4c | — license | the NX-OS license tier Cisco sells with the switch (Essentials covers this configuration) | 1 | |
| 4d | — console cable | RJ-45 to USB or DB-9 | 1 | Initial configuration |
| 5 | U200 to switch DAC | Cisco QSFP-100G-CU1M (100G passive copper, 1 m) | 1 | U200 QSFP0 to Eth1/49, run as 4 × 25G. Must run 25G per lane without FEC (CA-N); keep it 1–2 m. QSFP1 is unused. |
| 6 | QSFP28-to-SFP+ adapter (QSA) | Cisco CVR-QSFP-SFP10G | 1 | Eth1/51: holds the WAS-110 at 10G |
| 7 | Copper patch cables | Cat6 or Cat6A, lengths to suit | 6 | The CM2500's LAN 1 and LAN 2 to Eth1/46 and Eth1/47; server LAN, cluster and iDRAC to Eth1/1–3; the switch's mgmt0 to Eth1/40. More for LAN clients. |

## AT&T Fiber (WAN1)

| # | Item | Part / example | Qty | Notes |
| --- | --- | --- | --- | --- |
| 8 | XGS-PON ONT stick | WAS-110, **SC/APC** variant, from a reseller listed on [pon.wiki](https://pon.wiki/xgs-pon/ont/bfw-solutions/was-110/#value-added-resellers) (for example E.C.I. Networks EN-XGSFPP-OMAC-V2, FiberMall XGSPON-ONU-STICK, Azores Networks) | 1 | Runs the 8311 community firmware, masquerading as the BGW320 (README step 3). XGS-PON service only. |
| 9 | Fiber patch cable | SC/APC to SC/APC, simplex, single-mode OS2 9/125 µm (green connectors at both ends), length from AT&T's fiber jack to the switch | 1 | Never mate APC with UPC (blue) connectors |
| 10 | Fiber cleaner | one-click cleaner for 2.5 mm (SC) connectors | 1 | Clean both ends before each mating |
| — | AT&T BGW320 | supplied by AT&T | (1) | Not bought: its label and pages give the stick's values. Keep it in service until AT&T closes the installation ticket, then keep it as a spare. |

## Xfinity (WAN2)

| # | Item | Part / example | Qty | Notes |
| --- | --- | --- | --- | --- |
| 11 | DOCSIS 3.1 cable modem | NETGEAR Nighthawk CM2500 | 1 | Two 1G ports bonded with LACP to Eth1/46–47 (port-channel 46), up to 2 Gb/s. Check Xfinity's approved-device list for your plan. |
| 12 | Coax cable | RG6 with F connectors, from the wall outlet | 1 | Usually already there |

## Tools and one-time items

| # | Item | Notes |
| --- | --- | --- |
| 13 | Build machine with Vivado | Builds the `fpga_AU200_nat_ddr` bitstream. Its Vivado edition must support the xcu200 part. Vivado Lab Edition (free) is enough to flash a built image. |
| 14 | Laptop with an Ethernet port (or a USB Ethernet adapter) | Sets up the WAS-110 once (on the switch's setup port Eth1/45), and activates the CM2500 and turns on its aggregation (on its LAN 1) |
| 15 | Rack space and rails | 2U for the R7515 (Dell ReadyRails), 1U for the switch (rack kit included) |
| 16 | UPS (recommended) | Sized for the server, the switch and the modem. The gateway is the site's only route out. |

## Optional and fallback parts

| # | Item | When |
| --- | --- | --- |
| 17 | 10G SFP+ to 10GBASE-T media converter, plus a Cat6A cable | NX-OS refuses the WAS-110 in the QSA. The stick goes in the converter, and the converter goes to Eth1/45 (README step 3). |
| 18 | Spare DAC (item 5) and QSA (item 6) | Spares for the parts with no substitute on site |

## Software (no purchase)

- Proxmox VE 8 on the server; Ubuntu Server 24.04 LTS in the gateway VM.
- The natgw sources, built by the guest setup script: taxi `natgw-ddr4`, and dpdk and vpp `natgw`.
- The 8311 community firmware for the WAS-110 ([releases](https://github.com/djGrrr/8311-was-110-firmware-builder/releases/latest)).
