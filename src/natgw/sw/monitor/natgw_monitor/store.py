# SPDX-License-Identifier: BSD-3-Clause
"""Local storage: statistics in DuckDB (<state_dir>/stats.duckdb), events and
outages in SQLite (<state_dir>/monitor.db).

Statistics, three tiers:

  samples    every sample (1 s) of the graph series, for `raw_days` (30)
  rollup     per minute, min/avg/max of each graph series, for `retention_days` (365)
  counters   every `counter_minutes` (1), a snapshot of every counter, for
             `retention_days`. Rates over any window follow exactly from two
             snapshots; only peaks between snapshots are lost, and the
             rollup's max keeps those for the graphed series

DuckDB stores each column compressed and scans only the columns a query
needs, so long windows of 1 s data stay fast (a month's graph in tens of
milliseconds) and small. Values are stored as integers at a fixed scale per
series (rates in whole units; percentages, small rates and up/down
fractions in hundredths): whole numbers compress several times better than
full-precision floats. Tables are wide, a column per series, added when a
series first appears (a new WAN, a new port).

Events and outages stay in SQLite: they are small, written one at a time,
and an operator can read them with `sqlite3` while the monitor runs (DuckDB
locks its file to the monitor's process).

  events     WAN, outage, alert, VPP and monitor events, for `retention_days`
  outages    one row per ISP outage (end NULL while ongoing); open outages are
             never pruned

Samples are written in batches every `flush_seconds`, events at once. Both
databases journal: a crash loses at most the last batch and corrupts nothing.
The web server's threads read through their own connections (DuckDB
cursors, SQLite connections) while the sampler writes. Pruning runs hourly;
both reuse the freed space, so the files stop growing after the retention.
DuckDB's memory is capped at `memory_mb` (256): appends pin an uncompressed
block per column until a checkpoint, about 80 MB for these tables, so much
less than that fails writes. A fatal DuckDB error (which invalidates the
connection) reopens the database; at most the batch in flight is lost.

Sizes at the default retention, measured with realistic data: samples ~80 MB,
rollup ~64 MB, counters ~270 MB, events a few MB: about 410 MB in all."""

import json
import logging
import math
import os
import sqlite3
import threading
import time

import duckdb

log = logging.getLogger("natgw-monitor")

EVENTS_DB = "monitor.db"
STATS_DB = "stats.duckdb"
PRUNE_EVERY = 3600.0
TABLES = ("samples", "rollup", "counters")

# graph series kept in hundredths; everything else in whole units
HUNDREDTHS = {"ratio", "onchip", "ddr", "hit", "vr", "prom", "dem", "vpp"}


def scale_of(name):
    """the integer scale a graph series or counter is stored at"""
    base = name.rsplit("|", 1)[0]
    if base in HUNDREDTHS or base.endswith(".up") or base.startswith("vpp."):
        return 100
    return 1


def q(name):
    """an SQL identifier for any series name"""
    return '"' + name.replace('"', '""') + '"'


def _num(v):
    return v is not None and not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


class Store:
    def __init__(self, state_dir, raw_days=30.0, retention_days=365.0, counter_minutes=1, flush_seconds=10.0,
                 memory_mb=256, clock=time.time):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, EVENTS_DB)
        self.stats_path = os.path.join(state_dir, STATS_DB)
        self.raw_seconds = raw_days * 86400
        self.retention = retention_days * 86400
        self.counter_seconds = int(counter_minutes) * 60
        self.flush_seconds = flush_seconds
        self.clock = clock
        self.lock = threading.Lock()          # the writer connections and the buffers
        self.local = threading.local()
        self.w = self._sqlite()
        self._events_schema()
        self.memory_mb = int(memory_mb)
        self.gen = 0                          # +1 when the DuckDB connection is reopened
        self._open_stats()
        self.buf = []                         # [(t, {series: value})] not yet written
        self.roll_rows = []                   # finished minutes not yet written
        self.acc_minute = None                # minute being accumulated
        self.acc = {}                         # series -> [min, max, sum, n]
        self.minute_counters = None
        self.last_flush = clock()
        self.last_prune = 0.0

    # ------------------------------------------------------------ setup

    def _sqlite(self):
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _events_schema(self):
        w = self.w
        w.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                  "type TEXT NOT NULL, data TEXT NOT NULL)")
        w.execute("CREATE INDEX IF NOT EXISTS events_ts ON events (ts)")
        w.execute("CREATE TABLE IF NOT EXISTS outages (wan TEXT NOT NULL, start REAL NOT NULL, end REAL, "
                  "reason TEXT, snapshot TEXT, PRIMARY KEY (wan, start))")
        # statistics of earlier versions (SQLite) are left behind, not migrated
        old = [r[0] for r in w.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                       "('samples', 'rollup', 'counters')")]
        if old:
            for t in old:
                w.execute(f"DROP TABLE {t}")
            w.execute("VACUUM")
            log.info("dropped the statistics of an earlier version from %s (now in %s)", self.path, STATS_DB)

    def _open_stats(self):
        # appending pins an uncompressed 256 KB block per column of each
        # table's open row group (~300 columns: ~80 MB) until a checkpoint
        # compresses it: the limit must leave room for that
        self.d = duckdb.connect(self.stats_path)
        self.d.execute(f"SET memory_limit = '{self.memory_mb}MB'")
        self.d.execute("SET threads = 2")
        for t in TABLES:
            # no primary key: DuckDB would keep an index of every row in memory
            self.d.execute(f"CREATE TABLE IF NOT EXISTS {t} (t BIGINT NOT NULL)")
        self.cols = {t: self._columns(self.d, t) for t in TABLES}

    def _reopen_stats(self, e):
        """a fatal DuckDB error invalidates the connection until it is
        reopened; what was not committed is lost"""
        log.error("statistics database failed (%s); reopening it", e)
        try:
            self.d.close()
        except duckdb.Error:
            pass
        self.gen += 1
        try:
            self._open_stats()
        except duckdb.Error:
            log.exception("reopening %s", self.stats_path)

    @staticmethod
    def _columns(c, table):
        return {r[0] for r in c.execute(f"SELECT column_name FROM duckdb_columns() WHERE table_name = '{table}'"
                                        ).fetchall()} - {"t"}

    def _reader(self):
        """this thread's SQLite connection"""
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.execute("PRAGMA busy_timeout=10000")
            self.local.c = c
        return c

    def _dreader(self):
        """this thread's DuckDB cursor"""
        c = getattr(self.local, "d", None)
        if c is None or getattr(self.local, "gen", None) != self.gen:
            c = self.d.cursor()
            self.local.d = c
            self.local.gen = self.gen
        return c

    def _ensure(self, table, names):
        for n in names:
            if n not in self.cols[table]:
                self.d.execute(f"ALTER TABLE {table} ADD COLUMN {q(n)} BIGINT")
                self.cols[table].add(n)

    def close(self):
        """write everything, including the minute in progress (a restart
        within the same minute replaces that row with what follows)"""
        with self.lock:
            if self.acc or self.minute_counters:
                self._close_minute()
                self.acc_minute = None
            self._flush_locked()
            self.d.execute("CHECKPOINT")
            self.d.close()
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
                if not _num(v):
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
        snap = self.minute_counters if self.acc_minute % self.counter_seconds == 0 else None
        self.roll_rows.append((self.acc_minute, row, snap))
        self.acc = {}
        self.minute_counters = None

    def flush(self):
        with self.lock:
            self._flush_locked()

    def _insert(self, table, rows, replace=False):
        """rows: [(t, {column: value})], values scaled to integers; replace:
        rows already stored for those times go (a minute written at shutdown
        and again after a restart)"""
        if not rows:
            return
        names = sorted({k for _, r in rows for k in r})
        scales = [scale_of(n) for n in names]
        if replace:
            self.d.execute(f"DELETE FROM {table} WHERE t IN ({', '.join(str(int(t)) for t, _ in rows)})")
        # literal values, not bound parameters: every value is an integer or
        # NULL, and DuckDB's Python binding probes for optional modules
        # (NumPy, pandas) on every bound parameter, which costs ~20 ms a batch
        values = []
        for t, r in rows:
            vals = [str(int(t))]
            for n, sc in zip(names, scales):
                v = r.get(n)
                vals.append(str(round(v * sc)) if _num(v) else "NULL")
            values.append(f"({', '.join(vals)})")
        self.d.execute(f"INSERT INTO {table} (t{''.join(', ' + q(n) for n in names)}) VALUES {', '.join(values)}")

    def _flush_locked(self):
        self.last_flush = self.clock()
        if not self.buf and not self.roll_rows:
            return
        roll = [(m, r) for m, r, _ in self.roll_rows]
        cnt = [(m, c) for m, _, c in self.roll_rows if c]
        try:
            # new columns first: DDL outside the data transaction
            for table, rows in (("samples", self.buf), ("rollup", roll), ("counters", cnt)):
                self._ensure(table, sorted({k for _, r in rows for k in r}))
            self.d.execute("BEGIN")
            self._insert("samples", self.buf)
            self._insert("rollup", roll, replace=True)
            self._insert("counters", cnt, replace=True)
            self.d.execute("COMMIT")
        except duckdb.FatalException as e:
            self._reopen_stats(e)
        except duckdb.Error:
            log.exception("writing statistics")
            try:
                self.d.execute("ROLLBACK")
            except duckdb.Error:
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
            self.d.execute("BEGIN")
            self.d.execute("DELETE FROM samples WHERE t < ?", [int(now - self.raw_seconds)])
            self.d.execute("DELETE FROM rollup WHERE t < ?", [int(old)])
            self.d.execute("DELETE FROM counters WHERE t < ?", [int(old)])
            self.d.execute("COMMIT")
            self.d.execute("CHECKPOINT")      # frees the deleted blocks for reuse
        except duckdb.FatalException as e:
            self._reopen_stats(e)
        except duckdb.Error:
            log.exception("pruning statistics")
            try:
                self.d.execute("ROLLBACK")
            except duckdb.Error:
                pass
        try:
            self.w.execute("BEGIN")
            self.w.execute("DELETE FROM events WHERE ts < ?", (old,))
            self.w.execute("DELETE FROM outages WHERE end IS NOT NULL AND end < ?", (old,))
            self.w.execute("COMMIT")
            self.w.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            log.exception("pruning events")
            try:
                self.w.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def pending(self):
        """samples not yet written (the newest few seconds)"""
        with self.lock:
            return list(self.buf)

    # ------------------------------------------------------------ queries

    def _read_columns(self, table):
        return sorted(self._columns(self._dreader(), table))

    def history(self, since, until, max_points=600):
        """[(t, {series: avg}, {series: max})] for [since, until] in at most
        about max_points buckets, and the bucket length: from the 1 s samples
        for windows up to `raw_days`, else from the minute rollups"""
        c = self._dreader()
        span = max(until - since, 1)
        raw_from, roll_from = c.execute("SELECT (SELECT min(t) FROM samples), (SELECT min(t) FROM rollup)").fetchone()
        # the 1 s samples, unless they start later than the window and the
        # rollups go back further (raw_days shortened, or a fresh tier)
        # (a rollup's minute starts up to 59 s before the first sample in it)
        covered = raw_from is None or raw_from <= since + 60 or roll_from is None or roll_from >= raw_from - 60
        if span <= self.raw_seconds and covered:
            cols = self._read_columns("samples")
            b = max(1, math.ceil(span / max_points))
            sel = "".join(f", avg({q(n)}) / {scale_of(n)}, max({q(n)}) / {scale_of(n)}" for n in cols)
            rows = c.execute(f"SELECT (t // {b}) * {b} AS bt{sel} FROM samples WHERE t >= ? AND t <= ? "
                             f"GROUP BY bt ORDER BY bt", [int(since), int(until)]).fetchall()
            out = [(r[0], {n: r[1 + 2 * i] for i, n in enumerate(cols)},
                    {n: r[2 + 2 * i] for i, n in enumerate(cols)}) for r in rows]
            if b == 1:
                last = out[-1][0] if out else since - 1
                out += [(t, s, s) for t, s in self.pending() if last < t <= until]
            return out, b
        allc = self._read_columns("rollup")
        series = sorted({n.rsplit("|", 1)[0] for n in allc})
        b = max(60, math.ceil(span / max_points / 60) * 60)
        sel = "".join(f", avg({q(n + '|avg')}) / {scale_of(n)}, max({q(n + '|max')}) / {scale_of(n)}" for n in series)
        rows = c.execute(f"SELECT (t // {b}) * {b} AS bt{sel} FROM rollup WHERE t >= ? AND t <= ? "
                         f"GROUP BY bt ORDER BY bt", [int(since), int(until)]).fetchall()
        return [(r[0], {n: r[1 + 2 * i] for i, n in enumerate(series)},
                 {n: r[2 + 2 * i] for i, n in enumerate(series)}) for r in rows], b

    def oldest(self):
        """the time of the oldest stored sample (None: nothing stored)"""
        r = self._dreader().execute("SELECT least((SELECT min(t) FROM samples), (SELECT min(t) FROM rollup))"
                                    ).fetchone()
        return r[0]

    def counter_names(self):
        return self._read_columns("counters")

    def counter_history(self, name, since, until, max_points=600):
        """[(t, value)] of one counter: the last snapshot in each of at most
        about max_points buckets"""
        if name not in self._read_columns("counters"):
            return None
        b = max(self.counter_seconds, math.ceil((until - since) / max_points / 60) * 60)
        s = scale_of(name)
        rows = self._dreader().execute(
            f"SELECT max(t), arg_max({q(name)}, t) / {s} FROM counters WHERE t >= ? AND t <= ? AND {q(name)} IS NOT NULL "
            f"GROUP BY t // {b} ORDER BY 1", [int(since), int(until)]).fetchall()
        return [(t, int(v) if s == 1 else v) for t, v in rows]

    def info(self):
        c = self._reader()
        size = 0
        for p in (self.path, self.path + "-wal", self.stats_path, self.stats_path + ".wal"):
            if os.path.exists(p):
                size += os.path.getsize(p)
        r = self._dreader().execute("SELECT (SELECT min(t) FROM samples), (SELECT min(t) FROM rollup)").fetchone()
        return {"path": self.stats_path, "events_path": self.path, "bytes": size, "samples_from": r[0],
                "rollup_from": r[1], "events": c.execute("SELECT count(*) FROM events").fetchone()[0],
                "raw_days": self.raw_seconds / 86400, "retention_days": self.retention / 86400}

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
