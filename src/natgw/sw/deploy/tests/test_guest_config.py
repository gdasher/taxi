# SPDX-License-Identifier: BSD-3-Clause
"""natgw-guest-setup.sh's configuration rendering (guest_render.py):
natgw.cli, startup.conf and wanmgr.toml from deploy.env.example with
overrides."""

import pytest

from guest_render import render

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
    assert "weight = 5" in f["wanmgr.toml"].split('name = "xfinity"')[0]
    assert "weight = 2" in f["wanmgr.toml"].split('name = "xfinity"')[1]
    assert "nat44 plugin enable sessions 1048576" in cli
    assert "sync-budget 16384" in f["startup.conf"]
    assert "memory { main-heap-size 2G }" in f["startup.conf"]
    assert "  dev 0000:01:00.0\n" in f["startup.conf"]


ACL = "set acl-plugin acl "


def wan_acl(f):
    (line,) = [ln for ln in f["natgw.cli"].splitlines() if ln.startswith(ACL)]
    return [r.strip() for r in line[len(ACL):].split(",")]


def test_wan_acl_keeps_the_internet_off_the_management_addresses():
    assert wan_acl(render()) == [
        "permit src 0.0.0.0/0 dst 192.168.11.2/32",       # VPP itself: the WAS-110 pings it
        "deny src 0.0.0.0/0 dst 192.168.11.0/24",         # the stick's LCT
        "deny src 0.0.0.0/0 dst 192.168.100.1/32",        # the modem's page
        "deny src 0.0.0.0/0 dst 192.168.1.0/24",          # the LAN
        "permit src 0.0.0.0/0 dst 0.0.0.0/0"]


def test_wan_acl_for_plain_wans_and_unaligned_prefixes():
    assert wan_acl(render('NATGW_WAN1_ADDRESS=""', 'NATGW_WAN2_ROUTES=""')) == [
        "deny src 0.0.0.0/0 dst 192.168.1.0/24", "permit src 0.0.0.0/0 dst 0.0.0.0/0"]
    rules = wan_acl(render('NATGW_WAN2_ADDRESS="10.9.8.7/20"', 'NATGW_WAN2_ROUTES="172.16.5.9/12"'))
    assert rules[:2] == ["permit src 0.0.0.0/0 dst 192.168.11.2/32", "permit src 0.0.0.0/0 dst 10.9.8.7/32"]
    assert "deny src 0.0.0.0/0 dst 10.9.0.0/20" in rules and "deny src 0.0.0.0/0 dst 172.16.0.0/12" in rules


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
    ('NATGW_WAN2_ROUTES="192.168.100.1/32 modem"', "is not an address/prefix"),
    ('NATGW_WAN2_NAME="x\\"y"', "without quotes"),
    ('NATGW_WAN1_ADDRESS="192.168.311.2/24"', "is not an address/prefix"),
    ('NATGW_WAN2_ROUTES="192.168.100.1/33"', "is not an address/prefix"),
    ("NATGW_WAN1_WEIGHT=2.5", "whole number from 1 to 255"),
    ("NATGW_WAN2_WEIGHT=0", "whole number from 1 to 255"),
    ("NATGW_WAN2_WEIGHT=256", "whole number from 1 to 255"),
    ('NATGW_BULK_UDP_PORTS="70000"', "out of range"),
])
def test_bad_settings_are_refused(setting, message):
    with pytest.raises(RuntimeError, match=message):
        render(setting)
