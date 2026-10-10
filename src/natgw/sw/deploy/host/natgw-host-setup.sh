#!/bin/bash
# SPDX-License-Identifier: BSD-3-Clause
#
# natgw Proxmox VE host setup (run as root on the node that holds the U200,
# from a checkout of the taxi repository's natgw branch).
#
#   natgw-host-setup.sh prepare [--hugepages N] [--isolate CPULIST]
#       IOMMU and vfio for the card, N 1 GiB hugepages, optional CPU
#       isolation for the VM's pinned vCPUs; builds and installs pyrite,
#       the FPGA hook and natgw-fpga-update. Reboot the host afterwards.
#
#   natgw-host-setup.sh check
#       verify IOMMU, the card's IOMMU group, vfio binding and hugepages
#
#   natgw-host-setup.sh create-vm --vmid ID --storage STORAGE --bridge BRIDGE
#       --iso VOLUME [--cores N] [--memory MiB] [--disk GiB] [--affinity CPULIST]
#       create the gateway VM with the recommended settings (q35 with a
#       virtual IOMMU, host CPU, NUMA, 1 GiB hugepages, no ballooning, the card
#       passed through, the FPGA hook attached, started first at boot)
#
# See ../README.md for the whole procedure.

set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TAXI=$(cd "$HERE/../../../../.." && pwd)
CARD_ID=1234:c001
CONF=/etc/natgw/fpga.conf

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'natgw-host-setup: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root"
command -v qm >/dev/null || die "this is not a Proxmox VE host (no qm)"

card_bdf() { lspci -D -n -d "$CARD_ID" | awk '{print $1; exit}'; }

cpu_vendor() { awk -F': ' '/^vendor_id/ {print $2; exit}' /proc/cpuinfo; }

# kernel command line: systemd-boot (ZFS root on UEFI) or GRUB
set_cmdline() { # args: parameters to set (key=value); replaces earlier values of the same keys
	local file cur new keys
	keys=$(printf '%s\n' "$@" | sed 's/=.*//' | paste -sd'|')
	if [ -e /etc/kernel/cmdline ]; then
		file=/etc/kernel/cmdline
		cur=$(cat "$file")
	else
		file=/etc/default/grub
		cur=$(sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/\1/p' "$file")
	fi
	new=$(printf '%s\n' "$cur" | tr ' ' '\n' | grep -v -E "^($keys)=" | tr '\n' ' ')
	new=$(printf '%s %s' "$new" "$*" | tr -s ' ' | sed 's/^ //; s/ $//')
	[ "$cur" = "$new" ] && return 0
	echo "kernel command line: $new"
	if [ "$file" = /etc/kernel/cmdline ]; then
		echo "$new" > "$file"
		proxmox-boot-tool refresh
	else
		sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"$new\"|" "$file"
		update-grub
	fi
}

prepare() {
	local hugepages=0 isolate=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--hugepages) hugepages=$2; shift 2 ;;
		--isolate) isolate=$2; shift 2 ;;
		*) die "prepare: unknown option $1" ;;
		esac
	done

	say "IOMMU, hugepages and CPU isolation ($(cpu_vendor))"
	local params=(iommu=pt)
	# AMD (EPYC): the IOMMU is on by default; Intel needs intel_iommu=on on
	# older kernels
	[ "$(cpu_vendor)" = GenuineIntel ] && params+=(intel_iommu=on)
	if [ "$hugepages" -gt 0 ]; then
		params+=(default_hugepagesz=1G hugepagesz=1G "hugepages=$hugepages")
	fi
	if [ -n "$isolate" ]; then
		params+=("isolcpus=$isolate" "nohz_full=$isolate" "rcu_nocbs=$isolate")
	fi
	set_cmdline "${params[@]}"

	say "vfio for the card ($CARD_ID)"
	echo "options vfio-pci ids=$CARD_ID" > /etc/modprobe.d/natgw-vfio.conf
	for m in vfio vfio_iommu_type1 vfio_pci; do
		grep -qx "$m" /etc/modules || echo "$m" >> /etc/modules
	done
	# keep Xilinx XRT drivers off the card
	printf 'blacklist xclmgmt\nblacklist xocl\n' > /etc/modprobe.d/natgw-blacklist.conf
	update-initramfs -u -k all

	say "pyrite (flash tool), the FPGA hook and natgw-fpga-update"
	command -v gcc >/dev/null || { apt-get update -q; apt-get install -y -q build-essential; }
	make -C "$TAXI/src/pyrite/utils" -s
	install -m 0755 "$TAXI/src/pyrite/utils/pyrite" /usr/local/sbin/pyrite
	install -m 0755 "$HERE/natgw-fpga-update" /usr/local/sbin/natgw-fpga-update
	install -d /var/lib/vz/snippets /var/lib/natgw-fpga/pending /etc/natgw
	install -m 0755 "$HERE/natgw-fpga-hook.sh" /var/lib/vz/snippets/natgw-fpga-hook.sh
	if [ ! -e "$CONF" ]; then
		sed "s|^NATGW_CARD_BDF=.*|NATGW_CARD_BDF=$(card_bdf)|" "$HERE/fpga.conf.example" > "$CONF"
		echo "wrote $CONF (set NATGW_VMID once the VM exists)"
	fi
	# hookscripts live in a storage with the snippets content type
	local content
	content=$(pvesm config local 2>/dev/null | awk '/^\s*content/ {print $2}')
	case ",$content," in
	*,snippets,*) ;;
	*) pvesm set local --content "${content:+$content,}snippets" ;;
	esac

	say "done: reboot the host, then run '$0 check'"
}

check() {
	local ok=1 bdf grp
	say "check"
	if [ -d /sys/kernel/iommu_groups ] && [ -n "$(ls -A /sys/kernel/iommu_groups)" ]; then
		echo "IOMMU: on ($(find /sys/kernel/iommu_groups -mindepth 1 -maxdepth 1 | wc -l) groups)"
	else
		echo "IOMMU: OFF (enable AMD-Vi / VT-d and SR-IOV in the BIOS; check the kernel command line)"; ok=0
	fi
	bdf=$(card_bdf)
	if [ -z "$bdf" ]; then
		echo "card: not found ($CARD_ID): is the natgw bitstream in the card's flash?"; ok=0
	else
		echo "card: $bdf, $(lspci -s "$bdf" -vv 2>/dev/null | sed -n 's/.*LnkSta:\s*\(Speed [^,]*, Width [^,]*\).*/\1/p' | head -1)"
		grp=$(basename "$(readlink -f "/sys/bus/pci/devices/$bdf/iommu_group")")
		echo "IOMMU group $grp: $(find "/sys/kernel/iommu_groups/$grp/devices" -mindepth 1 -printf '%f ')"
		[ "$(find "/sys/kernel/iommu_groups/$grp/devices" -mindepth 1 | wc -l)" -eq 1 ] \
			|| { echo "  the group holds other devices: they must all be passed through together (try another slot)"; ok=0; }
		local drv=none
		[ -e "/sys/bus/pci/devices/$bdf/driver" ] && drv=$(basename "$(readlink -f "/sys/bus/pci/devices/$bdf/driver")")
		echo "driver: $drv"
		[ "$drv" = vfio-pci ] || ok=0
		echo "NUMA node of the slot: $(cat "/sys/bus/pci/devices/$bdf/numa_node")"
	fi
	echo "1 GiB hugepages: $(cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null || echo 0)"
	if command -v pyrite >/dev/null; then
		echo "pyrite: $(command -v pyrite)"
	else
		echo "pyrite: not installed"; ok=0
	fi
	if [ -x /var/lib/vz/snippets/natgw-fpga-hook.sh ]; then
		echo "hook: installed"
	else
		echo "hook: not installed"; ok=0
	fi
	if [ "$ok" -eq 1 ]; then
		echo "OK"
	else
		echo "NOT READY"; return 1
	fi
}

create_vm() {
	local vmid="" storage="" bridge="" iso="" cores=8 memory=16384 disk=32 affinity=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--vmid) vmid=$2; shift 2 ;;
		--storage) storage=$2; shift 2 ;;
		--bridge) bridge=$2; shift 2 ;;
		--iso) iso=$2; shift 2 ;;
		--cores) cores=$2; shift 2 ;;
		--memory) memory=$2; shift 2 ;;
		--disk) disk=$2; shift 2 ;;
		--affinity) affinity=$2; shift 2 ;;
		*) die "create-vm: unknown option $1" ;;
		esac
	done
	if [ -z "$vmid" ] || [ -z "$storage" ] || [ -z "$bridge" ] || [ -z "$iso" ]; then
		die "create-vm needs --vmid, --storage, --bridge and --iso"
	fi
	[ $((memory % 1024)) -eq 0 ] || die "--memory must be a multiple of 1024 MiB (1 GiB hugepages)"
	local bdf
	bdf=$(card_bdf)
	[ -n "$bdf" ] || die "card not found"

	say "creating VM $vmid (natgw)"
	# shellcheck disable=SC2054 # commas belong to Proxmox option values
	local args=(
		--name natgw --ostype l26
		--machine q35,viommu=intel --bios ovmf
		--efidisk0 "$storage:1,efitype=4m,pre-enrolled-keys=0"
		--cpu host --sockets 1 --cores "$cores" --numa 1
		--memory "$memory" --balloon 0 --hugepages 1024
		--scsihw virtio-scsi-single --scsi0 "$storage:$disk,iothread=1,discard=on"
		--net0 "virtio=52:54:00:4E:47:02,bridge=$bridge"
		--hostpci0 "$bdf,pcie=1"
		--ide2 "$iso,media=cdrom" --boot "order=scsi0;ide2"
		--serial0 socket
		--onboot 1 --startup order=1,up=60
		--hookscript local:snippets/natgw-fpga-hook.sh
	)
	[ -n "$affinity" ] && args+=(--affinity "$affinity")
	qm create "$vmid" "${args[@]}"
	sed -i "s|^NATGW_VMID=.*|NATGW_VMID=$vmid|" "$CONF"
	echo "VM $vmid created; $CONF updated. Not in HA: the VM cannot run on another node."
	qm config "$vmid"
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
prepare) prepare "$@" ;;
check) check ;;
create-vm) create_vm "$@" ;;
*) sed -n '4,22p' "$0"; exit 2 ;;
esac
