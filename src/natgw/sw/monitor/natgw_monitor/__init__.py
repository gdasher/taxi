# SPDX-License-Identifier: BSD-3-Clause
"""natgw monitor: statistics of the gateway (FPGA offload, flow tables, DDR
tier, NAT, VPP) and the state of the ISPs, served as a web page and JSON.

It only reads: VPP's stats segment (shared memory) and the WAN manager's
status file. It never calls into VPP's API or the device, so it cannot slow
down or disturb the datapath or rule installs."""
