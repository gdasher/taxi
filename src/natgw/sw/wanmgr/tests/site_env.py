# SPDX-License-Identifier: BSD-3-Clause
"""A test site: VPP (natgw_offload on the shim model) with a LAN and two
WANs, each WAN a 'modem' namespace running dnsmasq (DHCP with itself as
router) and holding the server 203.0.113.10 and two probe targets on lo, and
the WAN manager running as a daemon with alerts to a local SMTP sink. An ISP
outage is simulated by removing a WAN's upstream addresses (server and probe
targets) while its DHCP keeps working. Used by the WAN manager's and the
monitor's integration tests."""

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(SW, "vpp", "tests"))

import vppenv  # noqa: E402
from smtpsink import SmtpSink  # noqa: E402
from vppenv import Netns, Vpp, sudo, wait_for  # noqa: E402

TRAFFIC = os.path.join(SW, "vpp", "tests", "traffic.py")
SERVER = "203.0.113.10"
WANS = {
    "wan1": {"lane": 1, "if": "NatgwModel1", "modem": "198.51.100.2", "pool": ("198.51.100.100", "198.51.100.150"),
             "targets": ["192.0.2.1", "192.0.2.2"]},
    "wan2": {"lane": 2, "if": "NatgwModel2", "modem": "198.18.0.2", "pool": ("198.18.0.100", "198.18.0.150"),
             "targets": ["192.0.2.11", "192.0.2.12"]},
}
INTERVAL = 1.0


class Site:
    def __init__(self, name="wmgr", dpdk_extra="", wan_cli=()):
        """wan_cli: VPP commands run before the WANs come up, as the
        deployment's natgw.cli does (a cloned MAC, extra addresses, routes)"""
        self.wan_cli = list(wan_cli)
        self.tmp = tempfile.mkdtemp(prefix="wanmgr-")
        os.chmod(self.tmp, 0o777)
        self.smtp = SmtpSink()
        self.vpp = Vpp(name=name, extra_plugins=["dhcp_plugin.so"],
                       offload_conf="enable active-packets 4 sync-interval 0.25", dpdk_extra=dpdk_extra)
        self.ns = {}
        self.procs = []
        self.mgr = None
        self.status_path = os.path.join(self.tmp, "status.json")
        self.lan = Netns("wmgr_lan")
        self.ns["lan"] = self.lan
        for name in WANS:
            self.ns[name] = Netns(f"wmgr_{name}")
        self.wire()
        self.configure_vpp()
        self.start_manager()

    # ---- topology (repeatable after a VPP restart: the TAPs are new)

    def wire(self):
        v = self.vpp
        self.lan.run("ip", "link", "del", "eth0", check=False)
        self.lan.take(v.lane_tap(0), "eth0", "192.168.1.10/24")
        self.lan.run("ip", "route", "add", "default", "via", "192.168.1.1")
        for name, w in WANS.items():
            ns = self.ns[name]
            ns.run("ip", "link", "del", "eth0", check=False)
            ns.take(v.lane_tap(w["lane"]), "eth0", f"{w['modem']}/24")
            self.upstream(name, True)
            self.procs.append(self.dnsmasq(name, w))
            self.procs.append(self.server(ns, "udp-echo"))

    def dnsmasq(self, name, w):
        lo, hi = w["pool"]
        args = ["dnsmasq", "-k", "-C", "/dev/null", "--port=0", "--bind-interfaces", "--interface=eth0",
                "--except-interface=lo", f"--dhcp-range={lo},{hi},255.255.255.0,120",
                f"--dhcp-option=3,{w['modem']}", "--dhcp-authoritative",
                f"--dhcp-leasefile={self.tmp}/{name}.leases", f"--pid-file={self.tmp}/{name}.pid"]
        return self.ns[name].popen(*args)

    def server(self, ns, kind):
        p = ns.popen("python3", TRAFFIC, kind, "--bind", SERVER, "--port", "5000")
        assert p.stdout.readline().startswith("READY")
        return p

    def upstream(self, name, up):
        """the WAN's ISP side: server and probe targets reachable or not"""
        ns = self.ns[name]
        for a in [SERVER] + WANS[name]["targets"]:
            ns.run("ip", "addr", "add" if up else "del", f"{a}/32", "dev", "lo", check=False)

    def configure_vpp(self):
        v = self.vpp
        for c in self.wan_cli:
            v.cli(c)
        for c in ["set interface state NatgwModel0 up",
                  "set interface ip address NatgwModel0 192.168.1.1/24",
                  "nat44 plugin enable sessions 4096"]:
            v.cli(c)
        for w in WANS.values():
            v.cli(f"set interface state {w['if']} up")
            v.cli(f"set interface nat44 out {w['if']} output-feature")

    # ---- the manager

    def start_manager(self):
        cfg = f"""
[vpp]
api_socket = "{self.vpp.api_sock}"
api_dir = "{vppenv.API_DIR}"
[probe]
interval = {INTERVAL}
timeout = 0.2
up_rounds = 5
down_rounds = 3
[[wan]]
name = "wan1"
interface = "NatgwModel1"
probe_targets = {json.dumps(WANS['wan1']['targets'])}
[[wan]]
name = "wan2"
interface = "NatgwModel2"
probe_targets = {json.dumps(WANS['wan2']['targets'])}
[alerts]
smtp_host = "127.0.0.1"
smtp_port = {self.smtp.port}
sender = "natgw@test"
recipients = ["ops@test"]
max_per_hour = 100
syslog = false
hostname = "natgw-test"
"""
        path = os.path.join(self.tmp, "wanmgr.toml")
        with open(path, "w") as f:
            f.write(cfg)
        self.mgr_log = open(os.path.join(self.tmp, "wanmgr.log"), "w")
        env = dict(os.environ, PYTHONPATH=os.path.join(SW, "wanmgr"))
        # root, like the real daemon (VPP's API socket is root's)
        self.mgr = subprocess.Popen(["sudo", "-n", "--preserve-env=PYTHONPATH", sys.executable, "-m", "wanmgr",
                                     "-c", path, "--status-file", self.status_path, "-v"],
                                    stdout=self.mgr_log, stderr=subprocess.STDOUT, env=env)

    def status(self):
        try:
            with open(self.status_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def wan_state(self, name):
        st = self.status()
        return st and st["wans"][name]["state"]

    def lease(self, name):
        st = self.status()
        le = st and st["wans"][name]["lease"]
        return le and le["address"]

    def manager_log(self):
        with open(os.path.join(self.tmp, "wanmgr.log")) as f:
            return f.read()[-3000:]

    # ---- VPP state

    def api_default_paths(self):
        """gateways of the API-sourced default route ([] when absent)"""
        out = self.vpp.cli("show ip fib 0.0.0.0/0")
        m = re.search(r"\n  API refs:.*?(?=\n  \S|\n forwarding)", out, re.S)
        return sorted(re.findall(r"\n\s+(\d+\.\d+\.\d+\.\d+) NatgwModel\d", m.group(0))) if m else []

    def nat_pool(self):
        return sorted(re.findall(r"^(\d+\.\d+\.\d+\.\d+)$", self.vpp.cli("show nat44 addresses"), re.M))

    def offloaded(self):
        return int(re.search(r"offloaded sessions: (\d+)", self.vpp.cli("show natgw offload")).group(1))

    def clear_sessions(self):
        self.vpp.cli("clear nat44 ed sessions")
        wait_for(lambda: self.offloaded() == 0, what="sessions cleared")

    def mails(self):
        return [s.replace("[natgw-test] ", "") for s in self.smtp.subjects()]

    def udp(self, flows=8, count=4, phases=1, gap=0.0):
        r = self.lan.run("python3", TRAFFIC, "udp-client", "--dst", SERVER, "--port", "5000", "--flows", str(flows),
                         "--count", str(count), "--phases", str(phases), "--gap", str(gap), "--timeout", "1",
                         timeout=120)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def close(self):
        if self.mgr:
            kids = subprocess.run(["pgrep", "-P", str(self.mgr.pid)], capture_output=True, text=True).stdout.split()
            for pid in kids:
                sudo("kill", "-TERM", pid, check=False)
            try:
                self.mgr.wait(timeout=10)
            except subprocess.TimeoutExpired:
                for pid in kids:
                    sudo("kill", "-KILL", pid, check=False)
        for p in self.procs:
            p.kill()
        for ns in self.ns.values():
            ns.delete()
        self.vpp.stop()
        self.smtp.close()
        sudo("rm", "-rf", "--", self.tmp, check=False)


