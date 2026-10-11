#!/bin/bash
# SPDX-License-Identifier: BSD-3-Clause
#
# natgw gateway VM setup (Ubuntu Server 24.04, run as root).
#
# Builds and installs DPDK (with the cndm and natgw drivers), VPP (with the
# nat44-ed hooks and the natgw_offload plugin), the WAN manager and the
# monitor, writes the VPP, WAN manager, monitor, dnsmasq and
# management-network configuration from /etc/natgw/deploy.env, and sets up
# vfio, hugepages and the systemd services. Safe to run again: sources are
# updated and rebuilt, existing /etc/natgw/wanmgr.toml and the monitor's
# certificate are kept (/etc/natgw/monitor.toml is regenerated).
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
	-h|--help) sed -n '3,23p' "$0"; exit 0 ;;
	*) echo "unknown option $a" >&2; exit 2 ;;
	esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
ENV=/etc/natgw/deploy.env
CARD_ID=1234:c001
MONITOR_DUCKDB=1.5.5     # DuckDB for the monitor's statistics (see sw/monitor)

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
# added with the DDR tier: defaults for a deploy.env written before it
: "${NATGW_DPDK_BRANCH:=natgw}" "${NATGW_VPP_BRANCH:=natgw}" "${NATGW_VPP_HEAP:=2G}"
: "${NATGW_CARD_DEVARGS:=}" "${NATGW_BULK_UDP_PORTS:=}"
# per-WAN settings: defaults for a deploy.env written before them
: "${NATGW_WAN1_NAME:=wan1}" "${NATGW_WAN1_MAC:=}" "${NATGW_WAN1_ADDRESS:=}" "${NATGW_WAN1_ROUTES:=}"
: "${NATGW_WAN2_NAME:=wan2}" "${NATGW_WAN2_MAC:=}" "${NATGW_WAN2_ADDRESS:=}" "${NATGW_WAN2_ROUTES:=}"
: "${NATGW_WAN1_WEIGHT:=1}" "${NATGW_WAN2_WEIGHT:=1}"
: "${NATGW_MONITOR_BIND:=$NATGW_MGMT_IF}" "${NATGW_MONITOR_PORT:=8443}" "${NATGW_MONITOR_TLS:=yes}"
: "${NATGW_MONITOR_CERT:=}" "${NATGW_MONITOR_KEY:=}"

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
		dnsmasq-base dnsmasq ethtool openssl
	# dnsmasq must not serve the LAN before the cut-over (--apply-network)
	if [ "$APPLY_NETWORK" -eq 0 ]; then
		systemctl disable --now dnsmasq 2>/dev/null || true
	fi
}

# ---------------------------------------------------------------- sources

fetch_sources() {
	say "sources in $NATGW_SRC (taxi $NATGW_BRANCH, dpdk $NATGW_DPDK_BRANCH, vpp $NATGW_VPP_BRANCH)"
	install -d "$NATGW_SRC"
	local r b
	for r in taxi dpdk vpp; do
		case "$r" in
		taxi) b=$NATGW_BRANCH ;;
		dpdk) b=$NATGW_DPDK_BRANCH ;;
		vpp) b=$NATGW_VPP_BRANCH ;;
		esac
		if [ -d "$NATGW_SRC/$r/.git" ]; then
			# explicit refspec: the guide's clone is single-branch
			git -C "$NATGW_SRC/$r" fetch -q origin "+refs/heads/$b:refs/remotes/origin/$b"
			git -C "$NATGW_SRC/$r" checkout -q -B "$b" "origin/$b"
		else
			# shallow: the full DPDK and VPP histories are large
			git clone -q --depth 50 --branch "$b" "$NATGW_GIT_BASE/$r" "$NATGW_SRC/$r"
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
	# also keeps the mempool ops table from overflowing under VPP. VPP's dpdk
	# plugin includes the PCI, vdev and VMBus bus headers.
	local opts=(-Dplatform=generic -Denable_driver_sdk=true -Dtests=false -Denable_apps=test-pmd
		"-Denable_drivers=bus/pci,bus/vdev,bus/vmbus,mempool/ring,mempool/stack,common/natgw,net/cndm,net/natgw_model"
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

# ---------------------------------------------------------------- monitor

install_monitor() {
	say "monitor into $NATGW_PREFIX/monitor"
	install -d "$NATGW_PREFIX/monitor"
	rm -rf "$NATGW_PREFIX/monitor/natgw_monitor"
	cp -r "$NATGW_SRC/taxi/src/natgw/sw/monitor/natgw_monitor" "$NATGW_PREFIX/monitor/"
	find "$NATGW_PREFIX/monitor" -name __pycache__ -prune -exec rm -rf {} +
	# the statistics store (natgw_monitor/store.py); the version is pinned
	"$NATGW_PREFIX/venv/bin/pip" install -q "duckdb==$MONITOR_DUCKDB"
	PYTHONPATH="$NATGW_PREFIX/monitor" "$NATGW_PREFIX/venv/bin/python" -c "import natgw_monitor.store, natgw_monitor.web, vpp_papi.vpp_stats"
}

# the monitor's certificate: the one named in deploy.env, else a self-signed
# one created once (kept on later runs, so browsers' exceptions stay valid)
monitor_tls() {
	MON_TLS=false
	MON_CERT=/etc/natgw/tls/monitor.crt
	MON_KEY=/etc/natgw/tls/monitor.key
	case "$NATGW_MONITOR_TLS" in
	no) echo "monitor: plain HTTP (NATGW_MONITOR_TLS=no)"; return ;;
	yes) ;;
	*) die "NATGW_MONITOR_TLS must be yes or no" ;;
	esac
	MON_TLS=true
	if [ -n "$NATGW_MONITOR_CERT" ] || [ -n "$NATGW_MONITOR_KEY" ]; then
		[ -r "$NATGW_MONITOR_CERT" ] && [ -r "$NATGW_MONITOR_KEY" ] \
			|| die "NATGW_MONITOR_CERT and NATGW_MONITOR_KEY must both name readable files"
		MON_CERT=$NATGW_MONITOR_CERT
		MON_KEY=$NATGW_MONITOR_KEY
		echo "monitor: certificate $MON_CERT"
	elif [ ! -s "$MON_CERT" ] || [ ! -s "$MON_KEY" ]; then
		local name
		name=$(hostname)
		install -d -m 0750 -g natgw-mon /etc/natgw/tls
		openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
			-keyout "$MON_KEY" -out "$MON_CERT" -subj "/CN=$name natgw monitor" \
			-addext "subjectAltName=DNS:$name,DNS:localhost,IP:$NATGW_MGMT_IP,IP:127.0.0.1" 2>/dev/null
		echo "monitor: created a self-signed certificate ($MON_CERT), SHA-256 fingerprint:"
		openssl x509 -in "$MON_CERT" -noout -fingerprint -sha256 | sed 's/^/  /'
	else
		echo "monitor: keeping $MON_CERT"
	fi
	if [ "$MON_KEY" = /etc/natgw/tls/monitor.key ]; then
		chgrp natgw-mon "$MON_KEY"
		chmod 0640 "$MON_KEY"
	fi
	# the service runs as natgw-mon: it must be able to read both
	runuser -u natgw-mon -- test -r "$MON_CERT" && runuser -u natgw-mon -- test -r "$MON_KEY" \
		|| die "natgw-mon cannot read $MON_CERT and $MON_KEY (give its group read access)"
}

# ---------------------------------------------------------------- configuration

render() { # template -> stdout
	sed -e "s|@NATGW_PREFIX@|$NATGW_PREFIX|g" \
		-e "s|@NATGW_VPP_MAIN_CORE@|$NATGW_VPP_MAIN_CORE|g" \
		-e "s|@NATGW_CARD_BDF@|$CARD_BDF|g" \
		-e "s|@NATGW_LAN_IF@|$NATGW_LAN_IF|g" \
		-e "s|@NATGW_WAN1_IF@|$NATGW_WAN1_IF|g" \
		-e "s|@NATGW_WAN2_IF@|$NATGW_WAN2_IF|g" \
		-e "s|@NATGW_WAN1_NAME@|${WAN1_NAME_SED:-}|g" \
		-e "s|@NATGW_WAN2_NAME@|${WAN2_NAME_SED:-}|g" \
		-e "s|@NATGW_WAN1_CLI@|${WAN1_CLI:-}|g" \
		-e "s|@NATGW_WAN2_CLI@|${WAN2_CLI:-}|g" \
		-e "s|@NATGW_WAN_ACL@|${WAN_ACL:-}|g" \
		-e "s|@NATGW_WAN1_WEIGHT@|$NATGW_WAN1_WEIGHT|g" \
		-e "s|@NATGW_WAN2_WEIGHT@|$NATGW_WAN2_WEIGHT|g" \
		-e "s|@NATGW_LAN_CIDR@|$NATGW_LAN_CIDR|g" \
		-e "s|@NATGW_LAN_NET@|$NATGW_LAN_NET|g" \
		-e "s|@NATGW_LAN_GW@|$NATGW_LAN_GW|g" \
		-e "s|@NATGW_NAT_SESSIONS@|$NATGW_NAT_SESSIONS|g" \
		-e "s|@NATGW_VPP_HEAP@|$NATGW_VPP_HEAP|g" \
		-e "s|@NATGW_CARD_DEV_CONF@|${CARD_DEV_CONF:-}|g" \
		-e "s|@NATGW_OFFLOAD_CONF@|${OFFLOAD_CONF:-}|g" \
		-e "s|@NATGW_MGMT_IF@|$NATGW_MGMT_IF|g" \
		-e "s|@NATGW_MGMT_CIDR@|$NATGW_MGMT_CIDR|g" \
		-e "s|@NATGW_MGMT_IP@|$NATGW_MGMT_IP|g" \
		-e "s|@NATGW_DHCP_RANGE@|$NATGW_DHCP_RANGE|g" \
		-e "s|@NATGW_DNS_SERVERS@|$DNS_SERVERS|g" \
		-e "s|@NATGW_MONITOR_BIND_TOML@|${MON_BIND_TOML:-}|g" \
		-e "s|@NATGW_MONITOR_PORT@|$NATGW_MONITOR_PORT|g" \
		-e "s|@NATGW_MONITOR_TLS@|${MON_TLS:-true}|g" \
		-e "s|@NATGW_MONITOR_CERT@|${MON_CERT:-}|g" \
		-e "s|@NATGW_MONITOR_KEY@|${MON_KEY:-}|g" \
		"$HERE/templates/$1"
}

# prefix -> its network ("192.168.11.2/24" -> "192.168.11.0/24"); refuses
# anything that is not an IPv4 prefix (named by $2 in the error)
net_of() { # prefix setting
	local a b c d len n m
	[[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$ ]] \
		|| die "$2: '$1' is not an address/prefix"
	a=${BASH_REMATCH[1]} b=${BASH_REMATCH[2]} c=${BASH_REMATCH[3]} d=${BASH_REMATCH[4]} len=${BASH_REMATCH[5]}
	[ "$a" -le 255 ] && [ "$b" -le 255 ] && [ "$c" -le 255 ] && [ "$d" -le 255 ] && [ "$len" -le 32 ] \
		|| die "$2: '$1' is not an address/prefix"
	n=$(((a << 24) | (b << 16) | (c << 8) | d))
	m=$(((0xffffffff << (32 - len)) & 0xffffffff))
	n=$((n & m))
	echo "$((n >> 24 & 255)).$((n >> 16 & 255)).$((n >> 8 & 255)).$((n & 255))/$len"
}

# a WAN's own VPP commands: its MAC (a cloned one, for an ISP whose DHCP
# lease follows the old router's MAC), an extra address on its segment (to
# reach a device's management there, such as the WAS-110's LCT, whose
# management gateway is set to this address) and routes straight out of it
# (a cable modem's status page). Echoes the lines, "\n"-separated for render.
wan_cli() { # n
	local ifv="NATGW_WAN$1_IF" namev="NATGW_WAN$1_NAME" macv="NATGW_WAN$1_MAC"
	local addrv="NATGW_WAN$1_ADDRESS" routesv="NATGW_WAN$1_ROUTES" weightv="NATGW_WAN$1_WEIGHT"
	local ifn=${!ifv} out="" r
	case "${!namev}" in
	""|*[\"\\]*|*[![:print:]]*) die "$namev: give a name without quotes or backslashes" ;;
	esac
	[[ "${!weightv}" =~ ^[0-9]{1,3}$ ]] && [ "${!weightv}" -ge 1 ] && [ "${!weightv}" -le 255 ] \
		|| die "$weightv: '${!weightv}' is not a whole number from 1 to 255"
	if [ -n "${!macv}" ]; then
		[[ "${!macv}" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] || die "$macv: '${!macv}' is not a MAC address"
		out="${out}\\nset interface mac address $ifn ${!macv}"
	fi
	if [ -n "${!addrv}" ]; then
		net_of "${!addrv}" "$addrv" >/dev/null
		out="${out}\\nset interface ip address $ifn ${!addrv}"
	fi
	for r in ${!routesv}; do
		net_of "$r" "$routesv" >/dev/null
		out="${out}\\nip route add $r via $ifn"
	done
	echo "$out"
}

# the WAN ingress ACL's rules for the management addresses: nothing from the
# internet may reach the devices behind NATGW_WAN<n>_ADDRESS (its subnet,
# except VPP's own address, which the WAS-110 pings) or NATGW_WAN<n>_ROUTES.
# NAT drops such packets already (no session, NAT forwarding off); the ACL
# keeps them out even if "nat44 forwarding enable" is ever set. Replies to
# the LAN's sessions are addressed to the leases, so they pass. Echoes
# "rule, rule, " for the front of the ACL.
wan_acl() {
	local permits="" denies="" n addrv routesv r
	for n in 1 2; do
		addrv="NATGW_WAN${n}_ADDRESS" routesv="NATGW_WAN${n}_ROUTES"
		if [ -n "${!addrv}" ]; then
			permits="${permits}permit src 0.0.0.0/0 dst ${!addrv%/*}/32, "
			denies="${denies}deny src 0.0.0.0/0 dst $(net_of "${!addrv}" "$addrv"), "
		fi
		for r in ${!routesv}; do
			denies="${denies}deny src 0.0.0.0/0 dst $(net_of "$r" "$routesv"), "
		done
	done
	echo "$permits$denies"
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
	# the card's devargs (cndm PMD: natgw_ddr=0, tier policy)
	CARD_DEV_CONF=""
	[ -z "$NATGW_CARD_DEVARGS" ] || CARD_DEV_CONF=" { devargs $NATGW_CARD_DEVARGS }"
	# the offload plugin syncs sync-budget sessions a second: a pass over the
	# session limit in at most 64 s, well inside the UDP timeout (300 s).
	# Most of a large table is in DDR, whose flows sync without device reads.
	local budget=$((NATGW_NAT_SESSIONS / 64))
	[ "$budget" -ge 4096 ] || budget=4096
	OFFLOAD_CONF="\\n  sync-budget $budget"
	# sessions placed straight in the DDR tier
	local p
	for p in $NATGW_BULK_UDP_PORTS; do
		case "$p" in
		*[!0-9]*) die "NATGW_BULK_UDP_PORTS: '$p' is not a port number" ;;
		esac
		[ "$p" -ge 1 ] && [ "$p" -le 65535 ] || die "NATGW_BULK_UDP_PORTS: $p is out of range"
		OFFLOAD_CONF="${OFFLOAD_CONF}\\n  bulk-udp-port $p"
	done
	WAN1_CLI=$(wan_cli 1)
	WAN2_CLI=$(wan_cli 2)
	WAN_ACL=$(wan_acl)
	# names such as "AT&T": & and | are special in render's sed
	WAN1_NAME_SED=$(printf '%s' "$NATGW_WAN1_NAME" | sed 's/[&|]/\\&/g')
	WAN2_NAME_SED=$(printf '%s' "$NATGW_WAN2_NAME" | sed 's/[&|]/\\&/g')
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

	getent group natgw-mon >/dev/null || groupadd --system natgw-mon
	id natgw-mon >/dev/null 2>&1 || useradd --system --gid natgw-mon --groups vpp \
		--home-dir /var/lib/natgw-monitor --no-create-home --shell /usr/sbin/nologin natgw-mon
	usermod -a -G vpp natgw-mon
	monitor_tls
	MON_BIND_TOML=""
	for b in $NATGW_MONITOR_BIND; do
		MON_BIND_TOML="${MON_BIND_TOML:+$MON_BIND_TOML, }\"$b\""
	done
	[ -n "$MON_BIND_TOML" ] || die "NATGW_MONITOR_BIND is empty"
	render monitor.toml.in > /etc/natgw/monitor.toml
	chgrp natgw-mon /etc/natgw/monitor.toml
	chmod 0640 /etc/natgw/monitor.toml
	if [ -d "$NATGW_PREFIX/monitor/natgw_monitor" ]; then
		PYTHONPATH="$NATGW_PREFIX/monitor" "$NATGW_PREFIX/venv/bin/python" -m natgw_monitor --check \
			-c /etc/natgw/monitor.toml
	fi

	install -m 0755 "$HERE/natgw-card-check" /usr/local/sbin/natgw-card-check
	install -m 0755 "$HERE/natgw-fpga" /usr/local/sbin/natgw-fpga
	render vpp.service > /etc/systemd/system/vpp.service
	render natgw-wanmgr.service > /etc/systemd/system/natgw-wanmgr.service
	render natgw-monitor.service > /etc/systemd/system/natgw-monitor.service
	systemctl daemon-reload
	systemctl enable natgw-monitor.service
	# a running monitor picks up a new configuration or certificate
	systemctl try-restart natgw-monitor.service 2>/dev/null || true
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
	install_monitor
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
echo "Monitor:      $([ "$MON_TLS" = true ] && echo https || echo http)://<$NATGW_MONITOR_BIND>:$NATGW_MONITOR_PORT/ (/etc/natgw/monitor.toml, from deploy.env)"
if [ "$APPLY_NETWORK" -eq 0 ]; then
	echo "Network:      staged in /etc/natgw/60-natgw-mgmt.yaml and /etc/dnsmasq.d/natgw-lan.conf;"
	echo "              apply at cut-over with: $0 --no-build --apply-network"
fi
if [ "$REBOOT" -eq 1 ]; then
	echo "Reboot the VM to apply the kernel command line (IOMMU)."
fi
