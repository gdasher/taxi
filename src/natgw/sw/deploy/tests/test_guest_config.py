# SPDX-License-Identifier: BSD-3-Clause
"""natgw-guest-setup.sh's configuration rendering: its own render() and
configure() code (extracted from the script) on deploy.env.example with
overrides, writing natgw.cli, startup.conf and wanmgr.toml."""

import os
import subprocess

import pytest

GUEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guest")
SCRIPT = os.path.join(GUEST, "natgw-guest-setup.sh")

# the script's functions and configure()'s derived settings, without the
# rest of configure() (it installs files and users)
HARNESS = r'''
set -euo pipefail
G="$1"; shift
. "$G/deploy.env.example"
for kv in "$@"; do eval "$kv"; done
die() { echo "die: $*" >&2; exit 1; }
eval "$(sed -n '/^render() {/,/^}/p' "$G/natgw-guest-setup.sh")"
eval "$(sed -n '/^wan_cli() {/,/^}/p' "$G/natgw-guest-setup.sh")"
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


def cli_lines(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith("comment")]


def test_defaults_for_att_and_xfinity():
    f = render()
    cli = cli_lines(f["natgw.cli"])
    i1 = cli.index("set interface state CndmEthernet1 up")
    i2 = cli.index("set interface state CndmEthernet2 up")
    assert cli[i1 - 1] == "set interface ip address CndmEthernet1 192.168.11.2/24"
    assert cli[i2 - 1] == "ip route add 192.168.100.1/32 via CndmEthernet2"
    assert not any("mac address" in ln for ln in cli), "no MAC is cloned until one is set"
    assert 'name = "att"' in f["wanmgr.toml"] and 'name = "xfinity"' in f["wanmgr.toml"]
    assert "nat44 plugin enable sessions 1048576" in cli
    assert "sync-budget 16384" in f["startup.conf"]
    assert "memory { main-heap-size 2G }" in f["startup.conf"]
    assert "  dev 0000:01:00.0\n" in f["startup.conf"]


def test_cloned_mac_comes_before_the_interface_is_up():
    cli = cli_lines(render("NATGW_WAN1_MAC=00:1a:2b:3c:4d:5e")["natgw.cli"])
    i = cli.index("set interface mac address CndmEthernet1 00:1a:2b:3c:4d:5e")
    assert i < cli.index("set interface state CndmEthernet1 up")


def test_plain_wans_get_no_extra_commands():
    cli = cli_lines(render('NATGW_WAN1_ADDRESS=""', 'NATGW_WAN2_ROUTES=""')["natgw.cli"])
    i1 = cli.index("set interface state CndmEthernet1 up")
    assert cli[i1 - 1].startswith("set interface ip address CndmEthernet0")
    assert cli[i1 + 1] == "set interface state CndmEthernet2 up"


def test_routes_and_names_with_special_characters():
    f = render('NATGW_WAN1_NAME="AT&T Fiber|1"', 'NATGW_WAN2_ROUTES="192.168.100.1/32 10.0.0.0/8"')
    assert 'name = "AT&T Fiber|1"' in f["wanmgr.toml"]
    cli = cli_lines(f["natgw.cli"])
    assert "ip route add 10.0.0.0/8 via CndmEthernet2" in cli


def test_card_devargs_and_bulk_ports():
    f = render('NATGW_CARD_DEVARGS="natgw_ddr=0"', 'NATGW_BULK_UDP_PORTS="5201 9000"')
    assert "  dev 0000:01:00.0 { devargs natgw_ddr=0 }" in f["startup.conf"]
    assert "  bulk-udp-port 5201\n  bulk-udp-port 9000" in f["startup.conf"]


@pytest.mark.parametrize("setting,message", [
    ("NATGW_WAN1_MAC=00:1a:2b", "is not a MAC address"),
    ('NATGW_WAN1_ADDRESS="192.168.11.2"', "is not an address/prefix"),
    ('NATGW_WAN2_ROUTES="192.168.100.1/32 modem"', "is not a prefix"),
    ('NATGW_WAN2_NAME="x\\"y"', "without quotes"),
    ('NATGW_BULK_UDP_PORTS="70000"', "out of range"),
])
def test_bad_settings_are_refused(setting, message):
    with pytest.raises(RuntimeError, match=message):
        render(setting)
