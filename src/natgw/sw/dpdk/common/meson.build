# SPDX-License-Identifier: BSD-3-Clause
# natgw common driver: libnatgw, the shim software model and the rte_flow NAT
# backend, shared by the cndm and natgw_model PMDs

if not is_linux
    build = false
    reason = 'only supported on Linux'
    subdir_done()
endif

sources = files(
    'natgw_flow.c',
    'natgw_exports.c',
    'natgw/lib/natgw.c',
    'natgw/model/natgw_model.c',
)
includes += include_directories('.', 'natgw/include')
deps += ['ethdev', 'hash', 'mbuf']
cflags += ['-DNATGW_DPDK']
