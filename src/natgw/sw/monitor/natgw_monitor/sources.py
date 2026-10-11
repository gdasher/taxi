# SPDX-License-Identifier: BSD-3-Clause
"""Read-only data sources: VPP's stats segment and the WAN manager's status.

What VPP publishes there (VPP's own names, plus the natgw ones):

  /interfaces/<if>/xstats-natgw_*  the device's xstats (natgw_flow.c), copied
                                   by VPP's dpdk plugin every
                                   dpdk { stats-poll-interval } seconds
  /natgw/offload/<counter>         natgw_offload plugin counters, /natgw/offloaded
  /natgw/link/<if>                 DPDK port: bit 0 admin up, bit 1 link up
  /natgw/heartbeat                 +1 a second while VPP's main loop runs
  /if/names, /if/rx, /if/tx, ...   VPP interface counters (software path only:
                                   frames the FPGA forwards never reach VPP)
  /nat44-ed/*, /sys/*              NAT sessions, vector rates

vpp_papi reads gauges from its cached directory, so the directory is
refreshed on every read. Its reads wait without limit for VPP to finish an
update (a segment whose VPP died mid-update would hang the sampler, spinning),
so the client gets a lock with a deadline and reads are retried a few times
only: a segment that stays unreadable is VPP down, and is mapped afresh."""

import json
import os
import time

READ_TRIES = 5
LOCK_TIMEOUT = 0.2

XSTAT = "/xstats-natgw_"


class SourceDown(Exception):
    pass


def _sum_simple(v):
    """a simple counter vector summed over threads: [index] -> value"""
    if not v:
        return []
    out = [0] * max(len(t) for t in v)
    for t in v:
        for i, x in enumerate(t):
            out[i] += x
    return out


def _sum_combined(v):
    """[index] -> (packets, bytes)"""
    if not v:
        return []
    n = max(len(t) for t in v)
    p, b = [0] * n, [0] * n
    for t in v:
        for i, c in enumerate(t):
            p[i] += c[0]
            b[i] += c[1]
    return list(zip(p, b))


def _scalar(v):
    if isinstance(v, (list, tuple)):     # a symlink: per-thread values of one index
        return sum(v)
    return v


class DeadlineLock:
    """vpp_papi's optimistic stats lock, giving up after `timeout`"""

    def __init__(self, stats, timeout=LOCK_TIMEOUT):
        self.stats = stats
        self.timeout = timeout
        self.epoch = 0

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        self.epoch = self.stats.epoch
        while self.stats.in_progress:
            if time.monotonic() > deadline:
                raise IOError("stats segment update in progress for too long")
            time.sleep(0.001)
            self.epoch = self.stats.epoch
        return self

    def __exit__(self, *exc):
        if self.stats.in_progress or self.stats.epoch != self.epoch:
            raise IOError("stats segment changed during the read")


class StatsSource:
    """VPP's stats segment. read() returns one raw snapshot or raises
    SourceDown; it reconnects after VPP restarts."""

    def __init__(self, socket_path, client_factory=None, clock=time.monotonic, stale_after=5.0):
        self.path = socket_path
        self.factory = client_factory or self._vpp_stats
        self.clock = clock
        self.stale_after = stale_after
        self.c = None
        self.ino = None
        self.boottime = None
        self.heartbeat = None
        self.heartbeat_at = None

    @staticmethod
    def _vpp_stats(path):
        from vpp_papi.vpp_stats import VPPStats
        return VPPStats(socketname=path, timeout=2)

    def close(self):
        if self.c:
            try:
                self.c.disconnect()
            except Exception:
                pass
        self.c = None

    def _connect(self):
        try:
            st = os.stat(self.path)
        except OSError as e:
            self.close()
            raise SourceDown(f"no stats socket ({e.strerror})") from None
        # a restarted VPP's socket can reuse the old inode: its change time
        # tells them apart (the old mapping stays readable, frozen)
        ino = (st.st_ino, st.st_ctime_ns)
        if self.c is not None and ino == self.ino:
            return
        self.close()
        c = self.factory(self.path)
        try:
            c.connect()
        except Exception as e:
            raise SourceDown(f"stats socket: {e}") from None
        c.lock = DeadlineLock(c)
        self.c, self.ino = c, ino
        self.heartbeat = self.heartbeat_at = None

    def read(self):
        self._connect()
        try:
            for i in range(READ_TRIES):
                try:
                    return self._read()
                except IOError:
                    # VPP updated the segment while we read it: again
                    if i == READ_TRIES - 1:
                        raise
                    time.sleep(0.002)
        except SourceDown:
            raise
        except Exception as e:
            # the segment was remapped under us, or VPP went away mid-read
            self.close()
            raise SourceDown(f"stats segment: {e}") from None

    def _read(self):
        c = self.c
        c.refresh(blocking=False)
        d = c.directory

        def get(k):
            return c.__getitem__(k, blocking=False)
        boottime = get("/sys/boottime") if "/sys/boottime" in d else None
        if self.boottime is not None and boottime != self.boottime:
            # VPP restarted and reused the socket inode: map the new segment
            self.boottime = None
            self.close()
            self._connect()
            return self._read()
        self.boottime = boottime
        now = self.clock()
        hb = get("/natgw/heartbeat") if "/natgw/heartbeat" in d else None
        if hb != self.heartbeat:
            self.heartbeat, self.heartbeat_at = hb, now
        elif hb is not None and now - self.heartbeat_at > self.stale_after:
            # hung, or a segment we can no longer tell is old: map afresh next time
            stalled = now - self.heartbeat_at
            self.close()
            raise SourceDown(f"VPP not updating its statistics for {stalled:.0f} s")

        names = get("/if/names") if "/if/names" in d else []
        snap = {
            "boottime": boottime,
            "heartbeat": hb,
            "if_names": list(names),
            "if_rx": _sum_combined(get("/if/rx")) if "/if/rx" in d else [],
            "if_tx": _sum_combined(get("/if/tx")) if "/if/tx" in d else [],
            "if_drops": _sum_simple(get("/if/drops")) if "/if/drops" in d else [],
            "if_rx_miss": _sum_simple(get("/if/rx-miss")) if "/if/rx-miss" in d else [],
            "xstats": {},
            "offload": {},
            "link": {},
            "nat": {},
            "sys": {},
        }
        for k in d:
            if k.startswith("/interfaces/") and XSTAT in k:
                i = k.rindex(XSTAT)
                ifname = k[len("/interfaces/"):i]
                snap["xstats"].setdefault(ifname, {})[k[i + len(XSTAT):]] = _scalar(get(k))
            elif k.startswith("/natgw/offload/"):
                snap["offload"][k[len("/natgw/offload/"):]] = get(k)
            elif k.startswith("/natgw/link/"):
                snap["link"][k[len("/natgw/link/"):]] = get(k)
            elif k.startswith("/nat44-ed/"):
                v = get(k)
                snap["nat"][k[len("/nat44-ed/"):]] = sum(_sum_simple(v)) if isinstance(v, list) else v
        if "/natgw/offloaded" in d:
            snap["offload"]["offloaded"] = get("/natgw/offloaded")
        for k in ("vector_rate", "input_rate", "num_worker_threads"):
            if f"/sys/{k}" in d:
                snap["sys"][k] = get(f"/sys/{k}")
        return snap


class WanSource:
    """The WAN manager's status file (natgw-wanmgr --status-file)."""

    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path) as f:
                st = json.load(f)
        except FileNotFoundError:
            raise SourceDown("no status file (WAN manager not running?)") from None
        except (OSError, ValueError) as e:
            raise SourceDown(f"status file: {e}") from None
        if not isinstance(st, dict) or not isinstance(st.get("wans"), dict):
            raise SourceDown("status file: unexpected content")
        return st
