# SPDX-License-Identifier: BSD-3-Clause
"""Test environment: VPP on the net_natgw_model PMD (wire=tap) with Linux
network namespaces attached to the model's lanes.

Every lane of the model is a TAP interface owned by the VPP process; moving
it into a namespace makes that namespace the wire of the lane, so ordinary
kernel sockets generate the traffic the shim model and VPP see."""

import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time

VPP_PREFIX = os.environ.get("VPP_PREFIX", os.path.expanduser("~/src/vpp/install-natgw"))
VPP_BIN = os.path.join(VPP_PREFIX, "bin", "vpp")
PLUGIN_DIR = os.path.join(VPP_PREFIX, "lib", "x86_64-linux-gnu", "vpp_plugins")
API_DIR = os.path.join(VPP_PREFIX, "share", "vpp", "api")


def sudo(*args, check=True, capture=True, timeout=60, input=None):
    p = subprocess.run(["sudo", "-n"] + list(args), capture_output=capture, text=True, timeout=timeout,
                       input=input)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args)}: rc {p.returncode}: {p.stderr}")
    return p


def skip_reason():
    if not os.path.exists(VPP_BIN):
        return f"VPP not installed at {VPP_PREFIX}"
    if not os.path.exists(os.path.join(PLUGIN_DIR, "natgw_offload_plugin.so")):
        return "natgw_offload plugin not built"
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
        return "needs passwordless sudo"
    return None


class Netns:
    """A network namespace with one interface (a model lane's TAP)."""

    def __init__(self, name):
        self.name = name
        sudo("ip", "netns", "del", name, check=False)
        sudo("ip", "netns", "add", name)
        self.run("ip", "link", "set", "lo", "up")
        # ARP from the address on the target's subnet, not from the packet's
        # source (servers here live on lo)
        self.run("sysctl", "-qw", "net.ipv4.conf.all.arp_announce=2")

    def run(self, *args, check=True, timeout=60, input=None):
        return sudo("ip", "netns", "exec", self.name, *args, check=check, timeout=timeout, input=input)

    def popen(self, *args):
        return subprocess.Popen(["sudo", "-n", "ip", "netns", "exec", self.name] + list(args),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def take(self, ifname, newname, addr, mac=None):
        sudo("ip", "link", "set", ifname, "netns", self.name)
        self.run("ip", "link", "set", ifname, "name", newname)
        if mac:
            self.run("ip", "link", "set", newname, "address", mac)
        self.run("ip", "addr", "add", addr, "dev", newname)
        self.run("ip", "link", "set", newname, "up")
        # the model has no offloads: keep the kernel from sending GSO frames
        self.run("ethtool", "-K", newname, "tso", "off", "gso", "off", "gro", "off", check=False)

    def kill_all(self):
        """kill every process in the namespace (killing a popen() only ends
        its sudo)"""
        pids = sudo("ip", "netns", "pids", self.name, check=False).stdout.split()
        for pid in pids:
            sudo("kill", "-9", pid, check=False)

    def delete(self):
        self.kill_all()
        sudo("ip", "netns", "del", self.name, check=False)


class Vpp:
    """A VPP instance with the dpdk, nat and natgw_offload plugins and one
    net_natgw_model device."""

    def __init__(self, name="natgwt", lanes=3, bucket_w=10, offload_conf="enable", extra_plugins=(),
                 tap_prefix=None, vdev_args=""):
        self.name = name
        self.dir = tempfile.mkdtemp(prefix=f"{name}-")
        os.chmod(self.dir, 0o755)
        self.tap_prefix = tap_prefix or f"{name[:6]}t"
        self.lanes = lanes
        self.log = os.path.join(self.dir, "vpp.log")
        self.api_sock = os.path.join(self.dir, "api.sock")
        self.cli_sock = os.path.join(self.dir, "cli.sock")
        plugins = ["dpdk_plugin.so", "nat_plugin.so", "natgw_offload_plugin.so", "ping_plugin.so"]
        plugins += list(extra_plugins)
        conf = f"""
unix {{ nodaemon cli-listen {self.cli_sock} log {self.log} gid {os.getgid()} }}
api-segment {{ prefix {name} gid {os.getgid()} }}
socksvr {{ socket-name {self.api_sock} }}
buffers {{ buffers-per-numa 16384 }}
statseg {{ socket-name {self.dir}/stats.sock }}
plugins {{
  path {PLUGIN_DIR}
  plugin default {{ disable }}
{os.linesep.join(f"  plugin {p} {{ enable }}" for p in plugins)}
}}
dpdk {{
  no-pci
  vdev net_natgw_model0,lanes={lanes},wire=tap,tap_prefix={self.tap_prefix},bucket_w={bucket_w}{vdev_args}
}}
natgw-offload {{ {offload_conf} }}
"""
        self.conf = os.path.join(self.dir, "startup.conf")
        with open(self.conf, "w") as f:
            f.write(conf)
        self.console = os.path.join(self.dir, "console.log")
        self._console = open(self.console, "w")
        self.api = None
        self._start()

    def _start(self):
        cmd = [VPP_BIN, "-c", self.conf]
        if os.environ.get("VPP_GDB"):
            # debugging aid: a backtrace of every thread in the console on a crash
            cmd = ["gdb", "-q", "-batch", "-ex", "handle SIGUSR1 SIGPIPE nostop noprint", "-ex", "run",
                   "-ex", "thread apply all bt 20", "--args"] + cmd
        sudo("rm", "-f", self.api_sock, self.cli_sock, check=False)
        self.proc = subprocess.Popen(["sudo", "-n"] + cmd, stdout=self._console,
                                     stderr=subprocess.STDOUT, text=True)
        self._connect()

    def _connect(self, timeout=30):
        from vpp_papi import VPPApiClient
        deadline = time.time() + timeout
        while not os.path.exists(self.api_sock):
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f"VPP did not start (rc {self.proc.poll()}):\n{self.output()}")
            time.sleep(0.2)
        sudo("chmod", "666", self.api_sock)
        files = glob.glob(os.path.join(API_DIR, "**", "*.api.json"), recursive=True)
        self.api = VPPApiClient(apifiles=files, use_socket=True, server_address=self.api_sock)
        while True:
            try:
                self.api.connect(f"pytest-{os.getpid()}")
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        # wait for the main loop (the CLI is served from it)
        self.cli("show version")

    def output(self):
        """the tail of VPP's console (startup errors, crash backtraces)"""
        try:
            with open(self.console) as f:
                return "".join(ln for ln in f if "vat-plug/load" not in ln)[-6000:]
        except OSError:
            return ""

    def cli(self, cmd):
        r = self.api.api.cli_inband(cmd=cmd)
        if r.retval != 0:
            raise RuntimeError(f"vpp cli '{cmd}': {r.retval}: {r.reply}")
        return r.reply

    def lane_tap(self, lane):
        return f"{self.tap_prefix}{lane}"

    def sw_if_index(self, name):
        for i in self.api.api.sw_interface_dump():
            if i.interface_name == name:
                return i.sw_if_index
        raise KeyError(name)

    def if_counters(self, name):
        """(rx packets, tx packets) of an interface as VPP counts them"""
        out = self.cli(f"show interface {name}")
        rx = tx = 0
        for line in out.splitlines():
            f = line.split()
            for k in range(len(f) - 1):
                if f[k] == "rx" and f[k + 1] == "packets":
                    rx = int(f[k + 2])
                if f[k] == "tx" and f[k + 1] == "packets":
                    tx = int(f[k + 2])
        return rx, tx

    def offload_counters(self):
        out = self.cli("show natgw offload")
        c = {}
        for line in out.splitlines()[1:]:
            if ":" in line:
                k, v = line.rsplit(":", 1)
                c[k.strip()] = int(v)
        return c

    def _halt(self):
        try:
            if self.api:
                self.api.disconnect()
        except Exception:
            pass
        self.api = None
        if self.proc.poll() is None:
            # signal vpp itself: sudo does not relay signals from sudo'ed kill
            kids = subprocess.run(["pgrep", "-P", str(self.proc.pid)], capture_output=True,
                                  text=True).stdout.split()
            for sig in ("-TERM", "-KILL"):
                for pid in kids:
                    sudo("kill", sig, pid, check=False)
                try:
                    self.proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue

    def restart(self):
        """stop and start VPP with the same configuration and sockets (the
        model's TAP interfaces are recreated)"""
        self._halt()
        self._start()

    def stop(self):
        if self.proc.poll() is not None:
            # VPP died while the test ran: keep the evidence
            keep = os.path.join(tempfile.gettempdir(), f"{self.name}-crash-{int(time.time())}.log")
            shutil.copy(self.console, keep)
            print(f"VPP exited with {self.proc.returncode}; console saved to {keep}:\n{self.output()}")
        self._halt()
        self._console.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def wait_for(cond, timeout=10.0, interval=0.1, what="condition"):
    deadline = time.time() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(interval)
