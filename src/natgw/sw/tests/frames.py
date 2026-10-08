# SPDX-License-Identifier: BSD-3-Clause
"""Frame generators for libnatgw tests: every classification outcome, VLAN
and untagged, plus random malformed frames."""

import ipaddress
import random

from scapy.layers.inet import ICMP, IP, TCP, UDP
from scapy.layers.inet6 import IPv6
from scapy.layers.l2 import ARP, Dot1Q, Ether
from scapy.packet import Raw

GW_MAC = "02:00:00:00:aa:00"


def ip_str(v):
    return str(ipaddress.IPv4Address(v))


def l2(vlan=None, dst=GW_MAC, src="02:00:00:00:11:00"):
    p = Ether(dst=dst, src=src)
    if vlan is not None:
        p = p / Dot1Q(vlan=vlan, prio=random.randrange(8))
    return p


def flow(key, flags="A", payload=None, ttl=64, vlan=None):
    """Frame matching a natgw key (lane is the caller's choice of ingress)."""
    payload = payload if payload is not None else bytes(random.getrandbits(8) for _ in range(random.randint(0, 120)))
    l4 = TCP(sport=key.sport, dport=key.dport, flags=flags) if key.tcp else UDP(sport=key.sport, dport=key.dport)
    if vlan is None and key.vid:
        vlan = key.vid
    return bytes(l2(vlan) / IP(src=ip_str(key.sip), dst=ip_str(key.dip), ttl=ttl) / l4 / Raw(payload))


def exceptions():
    """At least one frame for each pre-lookup punt reason."""
    pay = Raw(b"x" * 20)
    out = [
        bytes(Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst="192.168.1.1") / pay),
        bytes(l2() / IPv6(src="fe80::1", dst="fe80::2") / UDP() / pay),
        bytes(l2(dst="01:00:5e:00:00:fb") / IP(src="10.0.0.1", dst="224.0.0.251") / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", flags="MF") / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", frag=10) / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", ttl=1) / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", ttl=0) / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / ICMP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", options=b"\x01\x01\x01\x00") / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", chksum=0x1234) / UDP() / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / TCP(flags="S") / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / TCP(flags="SA") / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / TCP(flags="FA") / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / TCP(flags="R") / pay),
        bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / TCP(flags="SF") / pay),
        bytes(l2(vlan=100) / Dot1Q(vlan=200) / IP(src="10.0.0.1", dst="8.8.8.8") / UDP() / pay),
        bytes(l2(vlan=10) / IP(src="10.0.0.1", dst="8.8.8.8", ttl=1) / UDP() / pay),
    ]
    # IP header errors: bad version, short IHL, total length beyond the frame, short L4
    base = bytearray(bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8") / UDP() / pay))
    bad_ver = bytearray(base); bad_ver[14] = 0x65; out.append(bytes(bad_ver))
    short_ihl = bytearray(base); short_ihl[14] = 0x44; out.append(bytes(short_ihl))
    long_len = bytearray(base); long_len[16:18] = (len(base) + 40).to_bytes(2, "big"); out.append(bytes(long_len))
    out.append(bytes(l2() / IP(src="10.0.0.1", dst="8.8.8.8", proto=6, len=36) / Raw(b"\x00" * 16)))
    return out


def garbage():
    n = random.choice([1, 13, 14, 17, 33, 42, 60, 61, 100])
    return bytes(random.getrandbits(8) for _ in range(n))


def random_key(lane=None, tcp=None, vid=0):
    return dict(lane=random.randrange(8) if lane is None else lane, vid=vid,
                tcp=random.randint(0, 1) if tcp is None else tcp,
                sip=random.getrandbits(32), dip=random.getrandbits(32),
                sport=random.getrandbits(16), dport=random.getrandbits(16))
