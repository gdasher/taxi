# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# NAT DDR tier: AXI4 clock converter between the shim (PCIe user clock,
# 250 MHz) and the DDR4 controller's user interface (300 MHz).

create_ip -name axi_clock_converter -vendor xilinx.com -library ip -module_name axi_cc_natgw_ddr

set_property -dict [list \
    CONFIG.PROTOCOL {AXI4} \
    CONFIG.DATA_WIDTH {512} \
    CONFIG.ADDR_WIDTH {34} \
    CONFIG.ID_WIDTH {4} \
    CONFIG.ACLK_ASYNC {1} \
    CONFIG.SYNCHRONIZATION_STAGES {3} \
    CONFIG.AWUSER_WIDTH {0} \
    CONFIG.ARUSER_WIDTH {0} \
    CONFIG.WUSER_WIDTH {0} \
    CONFIG.RUSER_WIDTH {0} \
    CONFIG.BUSER_WIDTH {0}
] [get_ips axi_cc_natgw_ddr]
