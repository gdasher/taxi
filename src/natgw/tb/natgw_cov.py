# SPDX-License-Identifier: CERN-OHL-S-2.0
"""
Functional coverage for the natgw testbenches.

Samples are taken in the shared layers (natgw_model's ShimModel and tables,
the DDR testbench monitor), so every testbench contributes without per-test
code. Each simulation writes natgw_cov.json into its own build directory (the
simulator's working directory); cov_report.py merges them and lists the bins
never hit. BINS defines every bin a point can have.
"""

import atexit
import itertools
import json
import os
from collections import Counter, defaultdict

RSN = ["miss", "bypass", "not_ipv4", "mcast", "ip_hdr", "frag", "ttl", "proto", "csum", "syn", "finrst",
       "vlan", "nh", "fwd"]

BINS = {
    # every outcome, untagged and tagged
    "outcome x vlan": [(r, v) for r in RSN for v in ("untagged", "vlan")],
    # every outcome on every lane
    "outcome x lane": [(r, l) for r in RSN for l in range(8)],
    # forwarded frames: translation direction x protocol x tagging x TTL decrement
    "forward kind": list(itertools.product(("snat", "dnat"), ("tcp", "udp"), ("untagged", "vlan"),
                                           ("dec_ttl", "keep_ttl"))),
    # which slot a hit came from
    "hit slot": [("uram", t, s) for t in (0, 1) for s in range(4)] + [("ddr", t, s) for t in (0, 1) for s in (0, 1)],
    # a FIN/RST on a hit, by tier
    "fin/rst hit": [("uram",), ("ddr",)],
    # punts with and without the punt header
    "punt header": [("on",), ("off",)],
    # cuckoo inserts: writes needed (1 = no relocation), and refusals
    "insert": [(tier, d) for tier in ("uram", "ddr") for d in ("1", "2", "3", "4", "5+", "full")],
    # DDR stage events (natgw_ddr testbench)
    "ddr event": [("lookup",), ("skip",), ("cap reached",), ("read error, lane",), ("read error, host",)],
}

_points = defaultdict(Counter)
_n = 0
_PATH = os.path.join(os.getcwd(), "natgw_cov.json")


try:
    import cocotb
    ENABLED = bool(getattr(cocotb, "is_simulation", False))
except ImportError:
    ENABLED = False


def hit(point, *b):
    """count one sample of bin b of point (only inside a simulation)"""
    global _n
    if not ENABLED:
        return
    _points[point][b] += 1
    _n += 1
    if _n % 2000 == 0:
        save()


def save():
    data = {p: [[list(b), n] for b, n in c.items()] for p, c in _points.items()}
    if data:
        with open(_PATH, "w") as f:
            json.dump(data, f)


atexit.register(save)
