# NAT gateway build floorplan (AU200)
#
# SLR1 holds the PCIe hard block and most of the cndm core; SLR2 holds the
# QSFP transceivers and MACs. The per-entry state memory (256 URAMs at 512k
# entries) and its logic go to SLR0, which is otherwise nearly empty, leaving
# the URAMs of SLR1/SLR2 to the lookup tables. The shim registers the hit
# stream and bubble request on both sides of this boundary.

create_pblock pblock_natgw_state
add_cells_to_pblock [get_pblocks pblock_natgw_state] [get_cells core_inst/natgw_inst/state_inst]
resize_pblock [get_pblocks pblock_natgw_state] -add {SLR0}
