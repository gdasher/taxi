# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# NAT DDR tier: DDR4 channel C2 reference clock (the controller is built with
# No_Buffer, so its input clock is constrained here). The crossings between
# the shim (PCIe user clock) and the controller's user clock go through the
# AXI clock converter and a taxi_sync_signal, which carry their own
# constraints.

create_clock -period 3.332 -name clk_ddr4_c2 [get_ports clk_ddr4_c2_p]
