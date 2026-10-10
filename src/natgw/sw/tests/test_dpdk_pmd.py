# SPDX-License-Identifier: BSD-3-Clause
"""Runs the DPDK-level rte_flow tests (dpdk/tests/test_natgw_pmd.c) against the
net_natgw_model PMD. Needs DPDK with the natgw drivers installed, hugepages and
passwordless sudo; skipped otherwise."""

import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTDIR = os.path.join(HERE, "..", "dpdk", "tests")
BIN = os.path.join(TESTDIR, "build", "test_natgw_pmd")

TESTS = ["ports", "validate_rejects", "snat_dnat_forwarding", "udp_zero_checksum", "punts", "count",
         "age", "destroy_and_duplicates", "fill_table", "host_tx", "no_punt_header", "concurrent",
         "ddr_spill", "ddr_punt_and_count", "ddr_age", "ddr_no_dimm", "ddr_disabled"]

EAL = ["-l", "0-1", "--no-pci", "--file-prefix", "natgw_pytest",
       "--vdev", "net_natgw_modelA,lanes=3,bucket_w=4,clock=manual",
       "--vdev", "net_natgw_modelB,lanes=2,punt_hdr=0,clock=manual",
       "--vdev", "net_natgw_modelC,lanes=2,bucket_w=2,ddr_bucket_w=6,clock=manual",
       "--vdev", "net_natgw_modelD,lanes=2,bucket_w=2,ddr_bucket_w=6,ddr_calib=0,clock=manual",
       "--vdev", "net_natgw_modelE,lanes=2,bucket_w=2,ddr_bucket_w=6,no_ddr=1,clock=manual"]


def _skip_reason():
    if not shutil.which("pkg-config") or subprocess.run(["pkg-config", "--exists", "libdpdk"]).returncode:
        return "DPDK (libdpdk.pc) not installed"
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
        return "needs passwordless sudo"
    return None


@pytest.fixture(scope="module")
def results():
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    subprocess.run(["make", "-s", "-C", TESTDIR], check=True)
    p = subprocess.run(["sudo", "-n", "timeout", "300", BIN] + EAL, capture_output=True, text=True)
    out = p.stdout + p.stderr
    status = dict((m.group(2), m.group(1)) for m in re.finditer(r"^(PASS|FAIL) (\w+)$", out, re.M))
    return p.returncode, status, out


@pytest.mark.parametrize("name", TESTS)
def test_pmd(results, name):
    _, status, out = results
    assert status.get(name) == "PASS", out


def test_clean_exit(results):
    rc, _, out = results
    assert rc == 0, out
    assert "Cannot close" not in out and "Invalid memory" not in out, out
