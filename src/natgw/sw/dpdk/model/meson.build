# SPDX-License-Identifier: BSD-3-Clause
# net_natgw_model: the natgw shim software model as a DPDK virtual device

if not is_linux
    build = false
    reason = 'only supported on Linux'
    subdir_done()
endif

sources = files('natgw_model_pmd.c')
deps += ['bus_vdev', 'kvargs', 'common_natgw']
cflags += ['-DNATGW_DPDK']
headers = files('rte_pmd_natgw_model.h')
require_iova_in_mbuf = false
