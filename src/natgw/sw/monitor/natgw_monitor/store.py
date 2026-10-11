# SPDX-License-Identifier: BSD-3-Clause
"""Local time-series and event storage (SQLite, <state_dir>/monitor.db).

Three tiers of statistics, and the event log:

  samples    every sample (1 s) of the graph series, for `raw_hours` (24 h)
  rollup     per minute, min/avg/max of each graph series, for `retention_days`
  counters   per minute, a snapshot of every counter, for `retention_days`.
             Rates over any window follow exactly from two snapshots; only
             peaks inside a minute are lost, and the rollup's max keeps those
             for the graphed series
  events     WAN, outage, alert, VPP and monitor events, for `retention_days`
  outages    one row per ISP outage (end NULL while ongoing); open outages are
             never pruned

The tables are wide (a column per series, added when a series first
appears: a new WAN, a new port) because that is several times smaller than a
row per value or a JSON document per sample. Samples are written in batches
every `flush_seconds`; events at once. WAL journaling: a crash or power loss
loses at most the last batch and never corrupts the file; the web server's
threads read through their own connections while the sampler writes. Pruning
runs hourly; SQLite reuses the freed pages, so the file stops growing after
the first `retention_days`.

Sizes (graph series ~29 values, ~190 counters; measured with random data,
real data with many zeros is smaller): samples ~17 MB, rollup ~6 MB,
counters ~13 MB, events well under 1 MB a week."""

import json
import logging
import math
import os
import sqlite3
import threading
import time

log = logging.getLogger("natgw-monitor")

DB_NAME = "monitor.db"
PRUNE_EVERY = 3600.0


def q(name):
    """an SQL identifier for any series name"""
    return '"' + name.replace('"', '""') + '"'


class Store:
    def __init__(self, state_dir, raw_hours=24.0, retention_days=7.0, flush_seconds=10.0, clock=time.time):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, DB_NAME)
        self.raw_seconds = raw_hours * 3600
        self.retention = retention_days * 86400
        self.flush_seconds = flush_seconds
        self.clock = clock
        self.lock = threading.Lock()          # the writer connection and the buffers
        self.local = threading.local()
        self.w = self._connect()
        self._schema()
        self.cols = {t: self._columns(t) for t in ("samples", "rollup", "counters")}
        self.buf = []                         # [(t, {series: value})] not yet written
        self.roll_rows = []                   # finished minutes not yet written
        self.acc_minute = None                # minute being accumulated
        self.acc = {}                         # series -> [min, max, sum, n]
        self.last_flush = clock()
        self.last_prune = 0.0
        self.minute_counters = None

    # ------------------------------------------------------------ setup

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _reader(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.execute("PRAGMA busy_timeout=10000")
            self.local.c = c
        return c

    def _schema(self):
        w = self.w
        w.execute("CREATE TABLE IF NOT EXISTS samples (t INTEGER PRIMARY KEY)")
        w.execute("CREATE TABLE IF NOT EXISTS rollup (t INTEGER PRIMARY KEY)")
        w.execute("CREATE TABLE IF NOT EXISTS counters (t INTEGER PRIMARY KEY)")
        w.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                  "type TEXT NOT NULL, data TEXT NOT NULL)")
        w.execute("CREATE INDEX IF NOT EXISTS events_ts ON events (ts)")
        w.execute("CREATE TABLE IF NOT EXISTS outages (wan TEXT NOT NULL, start REAL NOT NULL, end REAL, "
                  "reason TEXT, snapshot TEXT, PRIMARY KEY (wan, start))")

    def _columns(self, table):
        return {r[1] for r in self.w.execute(f"PRAGMA table_info({table})")} - {"t"}

    def _ensure(self, table, names):
        new = [n for n in names if n not in self.cols[table]]
        for n in new:
            self.w.execute(f"ALTER TABLE {table} ADD COLUMN {q(n)} REAL")
            self.cols[table].add(n)

    def close(self):
        """write everything, including the minute in progress (a restart
        within the same minute replaces that row with what follows)"""
        with self.lock:
            if self.acc or self.minute_counters:
                self._close_minute()
                self.acc_minute = None
            self._flush_locked()
            self.w.close()

    # ------------------------------------------------------------ statistics

    def add(self, t, series, counters=None):
        """one sample: the graph series ({name: number or None}) and,
        optionally, every counter ({name: number})"""
        with self.lock:
            self.buf.append((t, series))
            minute = int(t // 60) * 60
            if self.acc_minute is not None and minute != self.acc_minute:
                self._close_minute()
            self.acc_minute = minute
            for k, v in series.items():
                if v is None or (isinstance(v, float) and not math.isfinite(v)):
                    continue
                a = self.acc.get(k)
                if a is None:
                    self.acc[k] = [v, v, v, 1]
                else:
                    a[0] = min(a[0], v)
                    a[1] = max(a[1], v)
                    a[2] += v
                    a[3] += 1
            if counters is not None:
                self.minute_counters = counters
            if t - self.last_flush >= self.flush_seconds:
                self._flush_locked()
            if t - self.last_prune >= PRUNE_EVERY:
                self._prune_locked(t)

    def _close_minute(self):
        row = {}
        for k, (lo, hi, s, n) in self.acc.items():
            row[f"{k}|min"], row[f"{k}|avg"], row[f"{k}|max"] = lo, s / n, hi
        self.roll_rows.append((self.acc_minute, row, self.minute_counters))
        self.acc = {}
        self.minute_counters = None

    def flush(self):
        with self.lock:
            self._flush_locked()

    def _insert(self, table, rows):
        """rows: [(t, {column: value})], one transaction"""
        if not rows:
            return
        names = sorted({k for _, r in rows for k in r})
        self._ensure(table, names)
        sql = (f"INSERT OR REPLACE INTO {table} (t{''.join(', ' + q(n) for n in names)}) "
               f"VALUES (?{', ?' * len(names)})")
        self.w.executemany(sql, [[int(t)] + [r.get(n) for n in names] for t, r in rows])

    def _flush_locked(self):
        self.last_flush = self.clock()
        if not self.buf and not self.roll_rows:
            return
        try:
            self.w.execute("BEGIN")
            self._insert("samples", self.buf)
            self._insert("rollup", [(m, r) for m, r, _ in self.roll_rows])
            self._insert("counters", [(m, c) for m, _, c in self.roll_rows if c])
            self.w.execute("COMMIT")
        except sqlite3.Error:
            log.exception("writing statistics")
            try:
                self.w.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        self.buf = []
        self.roll_rows = []

    def prune(self, now=None):
        with self.lock:
            self._prune_locked(now if now is not None else self.clock())

    def _prune_locked(self, now):
        self.last_prune = now
        old = now - self.retention
        try:
            self.w.execute("BEGIN")
            self.w.execute("DELETE FROM samples WHERE t < ?", (int(now - self.raw_seconds),))
            self.w.execute("DELETE FROM rollup WHERE t < ?", (int(old),))
            self.w.execute("DELETE FROM counters WHERE t < ?", (int(old),))
            self.w.execute("DELETE FROM events WHERE ts < ?", (old,))
            self.w.execute("DELETE FROM outages WHERE end IS NOT NULL AND end < ?", (old,))
            self.w.execute("COMMIT")
            self.w.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            log.exception("pruning")
            try:
                self.w.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def pending(self):
        """samples not yet written (the newest few seconds)"""
        with self.lock:
            return list(self.buf)

    # ------------------------------------------------------------ queries

    def history(self, since, until, max_points=600):
        """[(t, {series: avg}, {series: max})] for [since, until], at most
        about max_points buckets: from the 1 s samples while they cover the
        window, else from the minute rollups"""
        c = self._reader()
        span = max(until - since, 1)
        if span <= self.raw_seconds:
            cols = [n for n in self._read_columns(c, "samples")]
            b = max(1, math.ceil(span / max_points))
            sel = "".join(f", avg({q(n)}), max({q(n)})" for n in cols)
            rows = c.execute(f"SELECT (t / {b}) * {b} AS bt{sel} FROM samples WHERE t >= ? AND t <= ? "
                             f"GROUP BY bt ORDER BY bt", (int(since), int(until))).fetchall()
            out = []
            for r in rows:
                avg = {n: r[1 + 2 * i] for i, n in enumerate(cols)}
                mx = {n: r[2 + 2 * i] for i, n in enumerate(cols)}
                out.append((r[0], avg, mx))
            if b == 1:
                last = out[-1][0] if out else since - 1
                out += [(t, s, s) for t, s in self.pending() if last < t <= until]
            return out, b
        allc = self._read_columns(c, "rollup")
        series = sorted({n.rsplit("|", 1)[0] for n in allc})
        b = max(60, math.ceil(span / max_points / 60) * 60)
        sel = ", ".join(f"avg({q(n + '|avg')}), max({q(n + '|max')})" for n in series)
        rows = c.execute(f"SELECT (t / {b}) * {b} AS bt{', ' + sel if sel else ''} FROM rollup "
                         f"WHERE t >= ? AND t <= ? GROUP BY bt ORDER BY bt", (int(since), int(until))).fetchall()
        return [(r[0], {n: r[1 + 2 * i] for i, n in enumerate(series)},
                 {n: r[2 + 2 * i] for i, n in enumerate(series)}) for r in rows], b

    @staticmethod
    def _read_columns(c, table):
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})") if r[1] != "t"]

    def oldest(self):
        """the time of the oldest stored sample (None: nothing stored)"""
        r = self._reader().execute("SELECT min(x) FROM (SELECT min(t) AS x FROM samples "
                                   "UNION ALL SELECT min(t) FROM rollup)").fetchone()
        return r[0]

    def counter_names(self):
        return sorted(self._read_columns(self._reader(), "counters"))

    def counter_history(self, name, since, until):
        """[(t, value)] of one counter, a snapshot a minute"""
        c = self._reader()
        if name not in self._read_columns(c, "counters"):
            return None
        return c.execute(f"SELECT t, {q(name)} FROM counters WHERE t >= ? AND t <= ? AND {q(name)} IS NOT NULL "
                         f"ORDER BY t", (int(since), int(until))).fetchall()

    def info(self):
        c = self._reader()
        size = sum(os.path.getsize(self.path + s) for s in ("", "-wal") if os.path.exists(self.path + s))
        r = c.execute("SELECT (SELECT min(t) FROM samples), (SELECT min(t) FROM rollup), "
                      "(SELECT count(*) FROM events)").fetchone()
        return {"path": self.path, "bytes": size, "samples_from": r[0], "rollup_from": r[1], "events": r[2],
                "raw_hours": self.raw_seconds / 3600, "retention_days": self.retention / 86400}

    # ------------------------------------------------------------ events and outages

    def add_event(self, rec):
        with self.lock:
            self.w.execute("INSERT INTO events (ts, type, data) VALUES (?, ?, ?)",
                           (rec.get("ts", self.clock()), rec.get("type", ""),
                            json.dumps(rec, separators=(",", ":"), default=str)))

    def events(self, limit=200, since=None):
        c = self._reader()
        if since is not None:
            rows = c.execute("SELECT data FROM events WHERE ts >= ? ORDER BY id", (since,)).fetchall()
        else:
            rows = c.execute("SELECT data FROM (SELECT id, data FROM events ORDER BY id DESC LIMIT ?) ORDER BY id",
                             (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def outage_open(self, wan, start, reason, snapshot):
        with self.lock:
            self.w.execute("INSERT OR REPLACE INTO outages (wan, start, end, reason, snapshot) VALUES (?, ?, NULL, ?, ?)",
                           (wan, start, reason, json.dumps(snapshot, default=str)))

    def outage_close(self, wan, start, end):
        with self.lock:
            self.w.execute("UPDATE outages SET end = ? WHERE wan = ? AND start = ?", (end, wan, start))

    def outages(self, limit=50, open_only=False):
        c = self._reader()
        where = "WHERE end IS NULL" if open_only else ""
        rows = c.execute(f"SELECT wan, start, end, reason, snapshot FROM outages {where} "
                         f"ORDER BY end IS NOT NULL, start DESC LIMIT ?", (limit,)).fetchall()
        return [{"wan": w, "start": s, "end": e, "duration": (e - s) if e is not None else None, "reason": r,
                 "snapshot": json.loads(sn) if sn else None} for w, s, e, r, sn in rows]

    def outages_between(self, since, until):
        """outages overlapping [since, until] (for marking graphs)"""
        c = self._reader()
        rows = c.execute("SELECT wan, start, end, reason FROM outages WHERE start <= ? AND (end IS NULL OR end >= ?) "
                         "ORDER BY start", (until, since)).fetchall()
        return [{"wan": w, "start": s, "end": e, "reason": r} for w, s, e, r in rows]

    # ------------------------------------------------------------ the old JSON-lines log

    def import_jsonl(self, path):
        """once: events.jsonl of earlier versions into the database (renamed
        to events.jsonl.imported afterwards)"""
        if not os.path.exists(path):
            return 0
        n = 0
        opened = {}
        with open(path) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                self.add_event(r)
                n += 1
                if r.get("type") == "outage_start":
                    opened[r["wan"]] = r
                    self.outage_open(r["wan"], r["ts"], r.get("reason"), r.get("snapshot"))
                elif r.get("type") == "outage_end" and r.get("wan") in opened:
                    o = opened.pop(r["wan"])
                    self.outage_close(r["wan"], o["ts"], r["ts"])
        os.replace(path, path + ".imported")
        log.info("imported %d events from %s", n, path)
        return n
