#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
# Add the natgw drivers (common_natgw, net_natgw_model, net_cndm) to a DPDK
# source tree by symlinking them in and listing them in the driver meson files.
# Usage: setup_dpdk.sh <dpdk-src>   (idempotent)
set -e
dpdk=$(cd "${1:?usage: $0 <dpdk-src>}" && pwd)
here=$(cd "$(dirname "$0")" && pwd)
taxi=$(cd "$here/../../../.." && pwd)

link() { [ -e "$2" ] || ln -s "$1" "$2"; }
link "$here/common" "$dpdk/drivers/common/natgw"
link "$here/model" "$dpdk/drivers/net/natgw_model"
link "$taxi/src/cndm/dpdk/cndm" "$dpdk/drivers/net/cndm"

add() { # add '<name>' to the drivers list of a meson.build
	grep -q "'$2'" "$1" || sed -i "0,/^drivers = \[/s//drivers = [\n        '$2',/" "$1"
}
add "$dpdk/drivers/common/meson.build" natgw
add "$dpdk/drivers/net/meson.build" natgw_model
add "$dpdk/drivers/net/meson.build" cndm
echo "natgw drivers added to $dpdk"
