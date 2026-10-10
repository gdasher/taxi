#!/bin/bash
# SPDX-License-Identifier: BSD-3-Clause
#
# natgw gateway VM setup (Ubuntu Server 24.04, run as root).
#
# Builds and installs DPDK (with the cndm and natgw drivers), VPP (with the
# nat44-ed hooks and the natgw_offload plugin) and the WAN manager, writes the
# VPP, WAN manager, dnsmasq and management-network configuration from
# /etc/natgw/deploy.env, and sets up vfio, hugepages and the systemd services.
# Safe to run again: sources are updated and rebuilt, existing
# /etc/natgw/wanmgr.toml is kept.
#
# Usage: natgw-guest-setup.sh [--apply-network] [--skip-hw] [--no-build]
#   --apply-network  cut-over: give the VM its static LAN address and start
#                    the LAN DHCP server (dnsmasq). Without it both are only
#                    staged, so an existing router can keep serving the LAN
#                    while the gateway is installed.
#   --skip-hw        no card, kernel or hugepage changes (container tests)
#   --no-build       skip fetching and building (configuration only)
#
# See ../README.md for the whole procedure.

set -euo pipefail

APPLY_NETWORK=0
SKIP_HW=0
BUILD=1
for a in "$@"; do
	case "$a" in
	--apply-network) APPLY_NETWORK=1 ;;
	--skip-hw) SKIP_HW=1 ;;
	--no-build) BUILD=0 ;;
	-h|--help) sed -n '3,22p' "$0"; exit 0 ;;
	*) echo "unknown option $a" >&2; exit 2 ;;
	esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
ENV=/etc/natgw/deploy.env
CARD_ID=1234:c001

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'natgw-guest-setup: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -r /etc/os-release ] && . /etc/os-release
[ "${ID:-}" = ubuntu ] || die "expected Ubuntu (24.04); found ${PRETTY_NAME:-unknown}"

if [ ! -r "$ENV" ]; then
	install -d /etc/natgw
	install -m 0644 "$HERE/deploy.env.example" "$ENV"
	die "wrote $ENV from the example: edit it, then run this script again"
fi
# shellcheck source=deploy.env.example
. "$ENV"

: "${NATGW_GIT_BASE:?}" "${NATGW_BRANCH:?}" "${NATGW_SRC:?}" "${NATGW_PREFIX:?}"
: "${NATGW_LAN_CIDR:?}" "${NATGW_LAN_NET:?}" "${NATGW_MGMT_IF:?}" "${NATGW_MGMT_CIDR:?}"
: "${NATGW_DHCP_RANGE:?}" "${NATGW_DNS_UPSTREAM:?}" "${NATGW_VPP_MAIN_CORE:?}" "${NATGW_NAT_SESSIONS:?}"
: "${NATGW_HUGEPAGES_2M:?}" "${NATGW_LAN_IF:?}" "${NATGW_WAN1_IF:?}" "${NATGW_WAN2_IF:?}"
: "${NATGW_IOMMU_MODE:=viommu}"

NATGW_LAN_GW=${NATGW_LAN_CIDR%/*}
NATGW_MGMT_IP=${NATGW_MGMT_CIDR%/*}
JOBS=$(nproc)

# ---------------------------------------------------------------- packages

install_packages() {
	say "packages"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -q
	apt-get install -y -q --no-install-recommends \
		build-essential git ca-certificates curl sudo pkg-config \
		meson ninja-build cmake python3 python3-venv python3-pip python3-pyelftools python3-ply \
		libnuma-dev libssl-dev libpcap-dev pciutils kmod iproute2 \
		dnsmasq-base dnsmasq ethtool
	# dnsmasq must not serve the LAN before the cut-over (--apply-network)
	if [ "$APPLY_NETWORK" -eq 0 ]; then
		systemctl disable --now dnsmasq 2>/dev/null || true
	fi
}

# ---------------------------------------------------------------- sources

fetch_sources() {
	say "sources in $NATGW_SRC (branch $NATGW_BRANCH)"
	install -d "$NATGW_SRC"
	for r in taxi dpdk vpp; do
		if [ -d "$NATGW_SRC/$r/.git" ]; then
			git -C "$NATGW_SRC/$r" fetch -q origin "$NATGW_BRANCH"
			git -C "$NATGW_SRC/$r" checkout -q -B "$NATGW_BRANCH" "origin/$NATGW_BRANCH"
		else
			# shallow: the full DPDK and VPP histories are large
			git clone -q --depth 50 --branch "$NATGW_BRANCH" "$NATGW_GIT_BASE/$r" "$NATGW_SRC/$r"
		fi
		echo "$r: $(git -C "$NATGW_SRC/$r" log --oneline -1)"
	done
}

# ---------------------------------------------------------------- DPDK

build_dpdk() {
	say "DPDK (generic x86-64 build with the natgw drivers)"
	local d="$NATGW_SRC/dpdk"
	# the natgw branch links drivers/net/cndm, drivers/net/natgw_model and
	# drivers/common/natgw into the taxi checkout next to it
	[ -e "$d/drivers/common/natgw/meson.build" ] || "$NATGW_SRC/taxi/src/natgw/sw/dpdk/setup_dpdk.sh" "$d"
	# platform=generic: never -march=native (the build host's CPU may not be
	# the server's). Only the drivers the gateway uses: fewer mempool drivers
	# also keeps the mempool ops table from overflowing under VPP.
	local opts=(-Dplatform=generic -Denable_driver_sdk=true -Dtests=false -Denable_apps=test-pmd
		"-Denable_drivers=bus/pci,bus/vdev,mempool/ring,mempool/stack,common/natgw,net/cndm,net/natgw_model"
		-Dprefix=/usr/local -Dbuildtype=release)
	if [ -d "$d/build" ]; then
		meson configure "$d/build" "${opts[@]}" >/dev/null
	else
		meson setup "$d/build" "$d" "${opts[@]}" >/dev/null
	fi
	ninja -C "$d/build" -j "$JOBS"
	meson install -C "$d/build" --quiet
	ldconfig
	pkg-config --modversion libdpdk
	if pkg-config --cflags libdpdk | grep -q -- '-march=native'; then
		die "DPDK was configured for the build host's CPU (-march=native)"
	fi
}

# ---------------------------------------------------------------- VPP

build_vpp() {
	say "VPP (nat44-ed hooks, natgw_offload plugin) into $NATGW_PREFIX/vpp"
	local v="$NATGW_SRC/vpp"
	# VPP's own build dependencies
	make -C "$v" install-dep CONFIRM=-y >/dev/null
	"$NATGW_SRC/taxi/src/natgw/sw/vpp/setup_vpp.sh" "$v" "$NATGW_PREFIX/vpp"
	[ -x "$NATGW_PREFIX/vpp/bin/vpp" ] || die "VPP did not install"
	[ -e "$NATGW_PREFIX/vpp/lib/x86_64-linux-gnu/vpp_plugins/natgw_offload_plugin.so" ] \
		|| die "the natgw_offload plugin was not built"
	ln -sf "$NATGW_PREFIX/vpp/bin/vppctl" /usr/local/bin/vppctl
}

# ---------------------------------------------------------------- WAN manager

install_wanmgr() {
	say "WAN manager into $NATGW_PREFIX/wanmgr"
	install -d "$NATGW_PREFIX/wanmgr"
	rm -rf "$NATGW_PREFIX/wanmgr/wanmgr"
	cp -r "$NATGW_SRC/taxi/src/natgw/sw/wanmgr/wanmgr" "$NATGW_PREFIX/wanmgr/"
	[ -x "$NATGW_PREFIX/venv/bin/python" ] || python3 -m venv "$NATGW_PREFIX/venv"
	"$NATGW_PREFIX/venv/bin/pip" install -q --upgrade "$NATGW_SRC/vpp/src/vpp-api/python"
	PYTHONPATH="$NATGW_PREFIX/wanmgr" "$NATGW_PREFIX/venv/bin/python" -c "import wanmgr, vpp_papi"
}

# ---------------------------------------------------------------- configuration

render() { # template -> stdout
	sed -e "s|@NATGW_PREFIX@|$NATGW_PREFIX|g" \
		-e "s|@NATGW_VPP_MAIN_CORE@|$NATGW_VPP_MAIN_CORE|g" \
		-e "s|@NATGW_CARD_BDF@|$CARD_BDF|g" \
		-e "s|@NATGW_LAN_IF@|$NATGW_LAN_IF|g" \
		-e "s|@NATGW_WAN1_IF@|$NATGW_WAN1_IF|g" \
		-e "s|@NATGW_WAN2_IF@|$NATGW_WAN2_IF|g" \
		-e "s|@NATGW_LAN_CIDR@|$NATGW_LAN_CIDR|g" \
		-e "s|@NATGW_LAN_NET@|$NATGW_LAN_NET|g" \
		-e "s|@NATGW_LAN_GW@|$NATGW_LAN_GW|g" \
		-e "s|@NATGW_NAT_SESSIONS@|$NATGW_NAT_SESSIONS|g" \
		-e "s|@NATGW_MGMT_IF@|$NATGW_MGMT_IF|g" \
		-e "s|@NATGW_MGMT_CIDR@|$NATGW_MGMT_CIDR|g" \
		-e "s|@NATGW_MGMT_IP@|$NATGW_MGMT_IP|g" \
		-e "s|@NATGW_DHCP_RANGE@|$NATGW_DHCP_RANGE|g" \
		-e "s|@NATGW_DNS_SERVERS@|$DNS_SERVERS|g" \
		"$HERE/templates/$1"
}

configure() {
	say "configuration"
	if [ "$SKIP_HW" -eq 1 ]; then
		CARD_BDF=${CARD_BDF:-0000:00:00.0}
		echo "(--skip-hw: card address placeholder $CARD_BDF)"
	else
		CARD_BDF=$(lspci -D -n -d "$CARD_ID" | awk '{print $1; exit}')
		[ -n "$CARD_BDF" ] || die "no natgw card ($CARD_ID) visible: pass the U200 through to this VM first"
		echo "card: $CARD_BDF"
	fi
	DNS_SERVERS=""
	for s in $NATGW_DNS_UPSTREAM; do
		DNS_SERVERS="${DNS_SERVERS}server=$s\\n"
	done

	getent group vpp >/dev/null || groupadd --system vpp
	install -d -m 0755 /etc/vpp /var/log/vpp /etc/natgw
	render startup.conf.in > /etc/vpp/startup.conf
	render natgw.cli.in > /etc/vpp/natgw.cli
	if [ -e /etc/natgw/wanmgr.toml ]; then
		echo "keeping /etc/natgw/wanmgr.toml"
	else
		render wanmgr.toml.in > /etc/natgw/wanmgr.toml
		chmod 0640 /etc/natgw/wanmgr.toml
	fi
	PYTHONPATH="$NATGW_PREFIX/wanmgr" "$NATGW_PREFIX/venv/bin/python" -m wanmgr --check -c /etc/natgw/wanmgr.toml

	install -d /etc/dnsmasq.d
	render dnsmasq-lan.conf.in | sed 's/\\n/\n/g' > /etc/dnsmasq.d/natgw-lan.conf
	render 60-natgw-mgmt.yaml.in > /etc/natgw/60-natgw-mgmt.yaml

	install -m 0755 "$HERE/natgw-card-check" /usr/local/sbin/natgw-card-check
	install -m 0755 "$HERE/natgw-fpga" /usr/local/sbin/natgw-fpga
	render vpp.service > /etc/systemd/system/vpp.service
	render natgw-wanmgr.service > /etc/systemd/system/natgw-wanmgr.service
	systemctl daemon-reload
}

# ---------------------------------------------------------------- kernel, vfio, hugepages

configure_hw() {
	say "vfio, IOMMU and hugepages"
	echo "options vfio-pci ids=$CARD_ID" > /etc/modprobe.d/natgw-vfio.conf
	printf 'vfio\nvfio-pci\n' > /etc/modules-load.d/natgw.conf
	echo "vm.nr_hugepages = $NATGW_HUGEPAGES_2M" > /etc/sysctl.d/80-natgw-hugepages.conf
	sysctl -q -p /etc/sysctl.d/80-natgw-hugepages.conf || true

	local args
	case "$NATGW_IOMMU_MODE" in
	viommu)
		# the VM's virtual IOMMU (Proxmox viommu=intel) is driven by the
		# intel_iommu driver whatever the host CPU (AMD EPYC included)
		args="intel_iommu=on iommu=pt"
		rm -f /etc/modprobe.d/natgw-noiommu.conf
		;;
	noiommu)
		args=""
		echo "options vfio enable_unsafe_noiommu_mode=1" > /etc/modprobe.d/natgw-noiommu.conf
		;;
	*) die "NATGW_IOMMU_MODE must be viommu or noiommu" ;;
	esac
	local cur new
	cur=$(sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/\1/p' /etc/default/grub)
	new=$(printf '%s\n' "$cur" | tr ' ' '\n' | grep -v -E '^(intel_iommu|iommu)=' | tr '\n' ' ')
	new=$(printf '%s %s' "$new" "$args" | tr -s ' ' | sed 's/^ //; s/ $//')
	if [ "$cur" != "$new" ]; then
		sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"$new\"|" /etc/default/grub
		update-grub
		REBOOT=1
	fi
	update-initramfs -u >/dev/null 2>&1 || true
	systemctl enable vpp.service natgw-wanmgr.service
}

apply_network() {
	say "cut-over: static management address and LAN DHCP"
	install -m 0600 /etc/natgw/60-natgw-mgmt.yaml /etc/netplan/60-natgw-mgmt.yaml
	netplan generate
	netplan apply
	systemctl enable --now dnsmasq
}

# ---------------------------------------------------------------- main

REBOOT=0
install_packages
if [ "$BUILD" -eq 1 ]; then
	fetch_sources
	build_dpdk
	build_vpp
	install_wanmgr
fi
configure
if [ "$SKIP_HW" -eq 0 ]; then
	configure_hw
fi
if [ "$APPLY_NETWORK" -eq 1 ]; then
	apply_network
fi

say "done"
echo "VPP:          $NATGW_PREFIX/vpp (config /etc/vpp/startup.conf, /etc/vpp/natgw.cli)"
echo "WAN manager:  /etc/natgw/wanmgr.toml (add SMTP settings for email alerts)"
if [ "$APPLY_NETWORK" -eq 0 ]; then
	echo "Network:      staged in /etc/natgw/60-natgw-mgmt.yaml and /etc/dnsmasq.d/natgw-lan.conf;"
	echo "              apply at cut-over with: $0 --no-build --apply-network"
fi
if [ "$REBOOT" -eq 1 ]; then
	echo "Reboot the VM to apply the kernel command line (IOMMU)."
fi
