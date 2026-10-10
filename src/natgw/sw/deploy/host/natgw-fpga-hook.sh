#!/bin/bash
# SPDX-License-Identifier: BSD-3-Clause
#
# Proxmox hookscript for the natgw gateway VM (installed in
# /var/lib/vz/snippets, attached with qm set <vmid> --hookscript
# local:snippets/natgw-fpga-hook.sh). Proxmox runs it as
# "natgw-fpga-hook.sh <vmid> <phase>".
#
# pre-start: if a bitstream is staged in /var/lib/natgw-fpga/pending, write it
# to the card's configuration flash (pyrite, verified), keep it as current, and
# reload the FPGA from flash (which hot-resets the PCIe port and rescans it);
# then make sure the card is bound to vfio-pci for the VM.
#
# The reload can only happen here, with the VM stopped: it drops the PCIe
# link, which a guest cannot recover from. A reboot from inside the guest
# keeps the same QEMU process and does not run this hook; use
# natgw-fpga-update --restart (or qm shutdown / qm start).

set -uo pipefail
VMID=${1:-}
PHASE=${2:-}
# paths can be overridden for tests
CONF=${NATGW_FPGA_CONF:-/etc/natgw/fpga.conf}
STATE=${NATGW_FPGA_STATE:-/var/lib/natgw-fpga}
SYSFS=${NATGW_SYSFS:-/sys}
CARD_ID=1234:c001

log() { echo "natgw-fpga: $*"; logger -t natgw-fpga -- "$*"; }

[ -r "$CONF" ] || { log "no $CONF: nothing to do"; exit 0; }
# shellcheck source=fpga.conf.example
. "$CONF"
[ "$VMID" = "${NATGW_VMID:-}" ] || exit 0
[ "$PHASE" = pre-start ] || exit 0

bdf=${NATGW_CARD_BDF:?set NATGW_CARD_BDF in $CONF}
dev=$SYSFS/bus/pci/devices/$bdf

fail() {
	log "ERROR: $*"
	if [ "${NATGW_STRICT:-no}" = yes ]; then
		exit 1
	fi
	log "starting the VM anyway (NATGW_STRICT=no)"
	exit 0
}

bind_vfio() {
	modprobe vfio-pci 2>/dev/null || true
	local drv=""
	[ -e "$dev/driver" ] && drv=$(basename "$(readlink -f "$dev/driver")")
	if [ "$drv" != vfio-pci ]; then
		[ -n "$drv" ] && echo "$bdf" > "$dev/driver/unbind"
		echo vfio-pci > "$dev/driver_override"
		echo "$bdf" > "$SYSFS/bus/pci/drivers_probe"
	fi
	[ -e "$dev/driver" ] && [ "$(basename "$(readlink -f "$dev/driver")")" = vfio-pci ]
}

[ -e "$dev" ] || fail "card $bdf not present on the host"
reload=${NATGW_RELOAD_ON_START:-no}

shopt -s nullglob
pending=("$STATE"/pending/*.bit "$STATE"/pending/*.bin)
if [ "${#pending[@]}" -gt 1 ]; then
	fail "more than one image staged in $STATE/pending; leave exactly one"
fi
if [ "${#pending[@]}" -eq 1 ]; then
	img=${pending[0]}
	log "flashing $(basename "$img") (sha256 $(sha256sum "$img" | cut -c1-16)) to $bdf"
	if pyrite -s "$bdf" -w "$img" -y >> "$STATE/flash.log" 2>&1; then
		install -d "$STATE/history"
		cp "$img" "$STATE/history/$(date +%Y%m%d-%H%M%S)-$(basename "$img")"
		mv -f "$img" "$STATE/current.${img##*.}"
		log "flash written and verified"
		reload=yes
	else
		mv -f "$img" "$img.failed"
		fail "pyrite could not write the flash (see $STATE/flash.log); the old image stays"
	fi
fi

if [ "$reload" = yes ]; then
	log "reloading the FPGA from flash"
	if ! pyrite -s "$bdf" -b -y >> "$STATE/flash.log" 2>&1; then
		fail "FPGA reload failed (see $STATE/flash.log)"
	fi
	for _ in 1 2 3 4 5 6 7 8 9 10; do
		[ -e "$dev/config" ] && break
		sleep 1
	done
	[ -e "$dev/config" ] || fail "card $bdf did not come back after the reload"
	lspci -n -s "$bdf" | grep -q "$CARD_ID" || fail "card $bdf came back with an unexpected ID: $(lspci -n -s "$bdf")"
fi

bind_vfio || fail "could not bind $bdf to vfio-pci"
log "card $bdf ready (vfio-pci)"
exit 0
