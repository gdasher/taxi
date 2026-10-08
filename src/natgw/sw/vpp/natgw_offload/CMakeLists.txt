# SPDX-License-Identifier: BSD-3-Clause
# natgw FPGA offload of nat44-ed sessions (needs the system DPDK with the
# natgw drivers: configure VPP with -DVPP_USE_SYSTEM_DPDK=ON)

find_package(PkgConfig)
pkg_check_modules(NGO_DPDK libdpdk)
if(NOT NGO_DPDK_FOUND)
  message(WARNING "-- libdpdk not found - natgw_offload plugin disabled")
  return()
endif()
include_directories(${NGO_DPDK_INCLUDE_DIRS})

add_vpp_plugin(natgw_offload
  SOURCES
  natgw_offload.c

  INSTALL_HEADERS
  natgw_offload.h

  LINK_LIBRARIES
  ${NGO_DPDK_LINK_LIBRARIES}
)
