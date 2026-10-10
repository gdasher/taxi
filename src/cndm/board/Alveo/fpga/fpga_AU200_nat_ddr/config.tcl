# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# NAT build with the DDR tier: the NAT build's configuration plus the DDR
# table size. NAT_DDR_BUCKET_W: 2 x 2^w lines of two entries
# (20 = 4M entries, 256 MB of the DIMM); override in the environment.

source ../fpga_AU200_nat/config.tcl

set nat_ddr_bucket_w 20
if {[info exists ::env(NAT_DDR_BUCKET_W)]} { set nat_ddr_bucket_w $::env(NAT_DDR_BUCKET_W) }
dict set params NAT_DDR_BUCKET_W $nat_ddr_bucket_w

set param_list {}
dict for {name value} $params {
    lappend param_list $name=$value
}
set_property generic $param_list [get_filesets sources_1]
