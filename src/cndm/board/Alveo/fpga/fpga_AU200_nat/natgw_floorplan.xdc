# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# NAT build floorplan.
#
# The QSFP transceivers sit at the top right of the device (SLR2, clock
# regions X5Y11-X5Y12). With the larger NAT tables (256k entries: 320 URAMs
# spread over SLR1 and SLR2) the placer pushed parts of the 390 MHz Ethernet
# MAC/PHY receive logic into SLR1, and those die crossings missed timing.
# Keep every lane's MAC/PHY in SLR2 next to its transceivers.

create_pblock pblock_eth_mac
add_cells_to_pblock [get_pblocks pblock_eth_mac] [get_cells -hierarchical -filter {NAME =~ core_inst/gt_quad[*].mac_inst}]
resize_pblock [get_pblocks pblock_eth_mac] -add {CLOCKREGION_X3Y10:CLOCKREGION_X5Y14}
