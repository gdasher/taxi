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
	# already applied when it reverses cleanly (e.g. on the natgw branch of
	# the VPP fork, which carries these commits)
	if git apply --reverse --check "$p" 2>/dev/null; then
		echo "already applied: $(basename "$p")"
	else
		git -c user.name="${GIT_AUTHOR_NAME:-natgw setup}" -c user.email="${GIT_AUTHOR_EMAIL:-natgw@localhost}" \
			am -q "$p"
	fi
done
[ -e src/plugins/natgw_offload ] || ln -s "$here/natgw_offload" src/plugins/natgw_offload

cmake -S src -B build-natgw -G Ninja -DCMAKE_BUILD_TYPE=release \
	-DVPP_USE_SYSTEM_DPDK=ON -DCMAKE_INSTALL_PREFIX="$prefix"
ninja -C build-natgw
ninja -C build-natgw install
echo "VPP installed to $prefix"
