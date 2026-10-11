# SPDX-License-Identifier: BSD-3-Clause
"""Renders the guest configuration with natgw-guest-setup.sh's own render()
and configure() code (extracted from the script), on deploy.env.example with
overrides. Used by test_guest_config.py and by the WAN manager's site tests,
which run the rendered natgw.cli in VPP."""

import os
import subprocess

GUEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guest")

# the script's functions and configure()'s derived settings, without the
# rest of configure() (it installs files and users)
HARNESS = r'''
set -euo pipefail
G="$1"; shift
. "$G/deploy.env.example"
for kv in "$@"; do eval "$kv"; done
die() { echo "die: $*" >&2; exit 1; }
eval "$(sed -n '/^render() {/,/^}/p' "$G/natgw-guest-setup.sh")"
for fn in net_of wan_cli wan_acl; do eval "$(sed -n "/^$fn() {/,/^}/p" "$G/natgw-guest-setup.sh")"; done
HERE=$G CARD_BDF=0000:01:00.0 NATGW_LAN_GW=192.168.1.1 NATGW_MGMT_IP=192.168.1.2
eval "derive() { $(sed -n '/# the card.s devargs/,/^\tdone$/p' "$G/natgw-guest-setup.sh")
$(sed -n '/^\tWAN1_CLI=/,/^\tdone$/p' "$G/natgw-guest-setup.sh"); }"
derive
for t in natgw.cli.in startup.conf.in wanmgr.toml.in; do echo "=== $t"; render "$t" | sed 's/\\n/\n/g'; done
'''


def render(*overrides):
    r = subprocess.run(["bash", "-c", HARNESS, "harness", GUEST, *overrides], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    files, cur = {}, None
    for line in r.stdout.splitlines():
        if line.startswith("=== "):
            cur = line[4:].removesuffix(".in")
            files[cur] = []
        else:
            files[cur].append(line)
    return {k: "\n".join(v) for k, v in files.items()}
