# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# NAT build with the DDR tier: the NAT build's configuration (256k on-chip
# entries) plus the DDR table size. NAT_DDR_BUCKET_W: 2 x 2^w lines of two entries
# (20 = 4M entries, 256 MB of the DIMM); override in the environment.

source ../fpga_AU200_nat/config.tcl

# On-chip size: the NAT build's 256k (NAT_BUCKET_W=15) meets timing with the
# compare-on-arrival DDR stage (WNS +0.011 ns). Fallback with more slack:
# NAT_BUCKET_W=14 (128k, WNS +0.019 ns) in the environment.

set nat_ddr_bucket_w 20
if {[info exists ::env(NAT_DDR_BUCKET_W)]} { set nat_ddr_bucket_w $::env(NAT_DDR_BUCKET_W) }
dict set params NAT_DDR_BUCKET_W $nat_ddr_bucket_w

set param_list {}
dict for {name value} $params {
    lappend param_list $name=$value
}
set_property generic $param_list [get_filesets sources_1]

# the DDR top-level ports, controller and clock converter (fpga_au200_nat.sv).
# Set on the fileset: a `define in the generated defines.v does not reach
# the other SystemVerilog files, each of which is its own compilation unit.
set_property verilog_define {NAT_DDR} [get_filesets sources_1]
