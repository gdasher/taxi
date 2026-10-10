#!/usr/bin/env python3
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""
Merge the natgw coverage files of every testbench run (natgw_cov.json under
each sim_build directory) and report every point's coverage and the bins never
hit. Exit status 1 when a point listed in --require is not complete.

  ./cov_report.py [--require "point,point"] [roots...]
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from natgw_cov import BINS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOTS = [HERE, os.path.join(HERE, "..", "..", "cndm", "board", "Alveo", "fpga", "tb", "fpga_core_nat")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--require", default="")
    a = ap.parse_args()
    total = defaultdict(Counter)
    by_tb = defaultdict(lambda: defaultdict(Counter))
    files = []
    for root in a.roots:
        files += glob.glob(os.path.join(root, "**", "sim_build", "*", "natgw_cov.json"), recursive=True)
        files += glob.glob(os.path.join(root, "sim_build", "*", "natgw_cov.json"))
    for f in sorted(set(files)):
        tb = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(f))))
        for point, bins in json.load(open(f)).items():
            for b, n in bins:
                total[point][tuple(b)] += n
                by_tb[point][tb][tuple(b)] += n
    print(f"{len(set(files))} coverage files")
    missing_req = []
    for point, bins in BINS.items():
        got = total.get(point, Counter())
        hitb = [b for b in bins if got.get(tuple(b), 0)]
        miss = [b for b in bins if not got.get(tuple(b), 0)]
        pct = 100.0 * len(hitb) / len(bins)
        tbs = ", ".join(sorted(by_tb[point])) or "none"
        print(f"{point:24s} {len(hitb):4d}/{len(bins):<4d} {pct:5.1f}%   from: {tbs}")
        if miss:
            print("    never hit: " + "; ".join(" ".join(str(x) for x in b) for b in miss[:24]) +
                  (f" (+{len(miss) - 24} more)" if len(miss) > 24 else ""))
        if point in a.require.split(",") and miss:
            missing_req.append(point)
    if missing_req:
        print("incomplete required points: " + ", ".join(missing_req))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
