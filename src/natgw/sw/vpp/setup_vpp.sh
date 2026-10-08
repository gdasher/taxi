#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
# Prepare a VPP source tree for natgw: apply the patches (nat44-ed session
# hooks, dpdk plugin driver names and DPDK 26.03 compatibility), link the
# natgw_offload plugin in, and build against the system DPDK (which must
# have the natgw drivers: see ../dpdk/setup_dpdk.sh).
# Usage: setup_vpp.sh <vpp-src> [install-prefix]   (idempotent)
set -e
vpp=$(cd "${1:?usage: $0 <vpp-src> [prefix]}" && pwd)
prefix=${2:-$vpp/install-natgw}
here=$(cd "$(dirname "$0")" && pwd)

cd "$vpp"
for p in "$here"/patches/*.patch; do
	subject=$(sed -n 's/^Subject: \[PATCH[^]]*\] //p' "$p")
	if git log --format=%s | grep -qxF "$subject"; then
		echo "already applied: $subject"
	else
		git am -q "$p"
	fi
done
[ -e src/plugins/natgw_offload ] || ln -s "$here/natgw_offload" src/plugins/natgw_offload

cmake -S src -B build-natgw -G Ninja -DCMAKE_BUILD_TYPE=release \
	-DVPP_USE_SYSTEM_DPDK=ON -DCMAKE_INSTALL_PREFIX="$prefix"
ninja -C build-natgw
ninja -C build-natgw install
echo "VPP installed to $prefix"
