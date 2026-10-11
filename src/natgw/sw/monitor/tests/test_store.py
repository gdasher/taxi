# SPDX-License-Identifier: BSD-3-Clause
"""The SQLite store: tiers, rollups, retention, batching, concurrency,
crash safety, the old event log import, and size."""

import json
import math
import os
import random
import sqlite3
import threading

from monfakes import Clock
from natgw_monitor import config
from natgw_monitor.collector import Collector
from natgw_monitor.store import Store
from monfakes import FakeStats, FakeWans

T0 = 1_700_000_040      # a minute boundary


def rows(tmp_path, sql, st=None):
    """statistics through the store's own DuckDB connection (the file is
    locked to it); events and outages from monitor.db"""
    if st is not None:
        st.flush()
        return st.d.execute(sql).fetchall()
    return sqlite3.connect(tmp_path / "monitor.db").execute(sql).fetchall()


def make(tmp_path, **kw):
    clock = Clock(T0)
    return Store(str(tmp_path), clock=clock, **kw), clock


def feed(st, clock, n, series=lambda i: {"a": float(i)}, counters=lambda i: {"c": 1000 + i}, start=0):
    for i in range(start, start + n):
        clock.t = T0 + i
        st.add(clock.t, series(i), counters(i))


def test_tiers_rollup_and_counter_snapshots(tmp_path):
    st, clock = make(tmp_path)
    feed(st, clock, 181, series=lambda i: {"a": float(i), "b": None if i % 2 else 2.0})
    assert rows(tmp_path, "SELECT count(*) FROM samples", st)[0][0] == 181
    # stored as integers: "a" in whole units, the avg rounded
    r = rows(tmp_path, 'SELECT t, "a|min", "a|avg", "a|max", "b|avg" FROM rollup ORDER BY t', st)
    # three whole minutes (the 181st sample opened a fourth)
    assert r == [(T0, 0, 30, 59, 2), (T0 + 60, 60, 90, 119, 2), (T0 + 120, 120, 150, 179, 2)]
    # the counters as they were at the end of each minute
    assert rows(tmp_path, "SELECT t, c FROM counters ORDER BY t", st) == [(T0, 1059), (T0 + 60, 1119), (T0 + 120, 1179)]
    assert st.counter_history("c", T0, T0 + 600) == [(T0, 1059), (T0 + 60, 1119), (T0 + 120, 1179)]
    assert st.counter_history("nope", T0, T0 + 600) is None


def test_writes_are_batched_and_pending_is_served(tmp_path):
    st, clock = make(tmp_path, flush_seconds=10)
    feed(st, clock, 5)
    assert st.d.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    h, b = st.history(T0, T0 + 4, max_points=600)
    assert b == 1 and [t for t, _, _ in h] == [T0 + i for i in range(5)], "unwritten samples still served"
    feed(st, clock, 10, start=5)
    assert st.d.execute("SELECT count(*) FROM samples").fetchone()[0] >= 10


def test_history_buckets_from_samples_then_rollups(tmp_path):
    st, clock = make(tmp_path, raw_days=0.05 / 24)      # 3 minutes of 1 s samples
    # "hit" is stored in hundredths, so its minute averages keep their halves
    feed(st, clock, 600, series=lambda i: {"hit": float(i % 10)})
    st.flush()
    h, b = st.history(T0 + 420, T0 + 599, max_points=60)
    assert b == 3 and all(m["hit"] >= a["hit"] for _, a, m in h)
    t, a, m = h[0]
    assert t == T0 + 420 and a["hit"] == 1.0 and m["hit"] == 2.0       # samples 0, 1, 2 of the cycle
    h, b = st.history(T0, T0 + 599, max_points=600)                     # longer than the raw tier
    assert b == 60 and len(h) == 9 and h[0][1]["hit"] == 4.5 and h[0][2]["hit"] == 9.0


def test_new_series_and_odd_names(tmp_path):
    st, clock = make(tmp_path)
    feed(st, clock, 3, series=lambda i: {"a": 1.0})
    feed(st, clock, 3, series=lambda i: {"a": 1.0, 'w.my "isp".rx': 5.0}, start=3)
    st.flush()
    h, _ = st.history(T0, T0 + 5)
    assert h[-1][1]['w.my "isp".rx'] == 5.0 and h[0][1]['w.my "isp".rx'] is None


def test_retention(tmp_path):
    st, clock = make(tmp_path, raw_days=1 / 24, retention_days=1)
    feed(st, clock, 121)               # two whole minutes
    st.add_event({"ts": T0, "type": "monitor"})
    st.outage_open("wan1", T0, "link down", {})
    st.outage_open("wan2", T0 + 1, "no DHCP lease", {})
    st.outage_close("wan2", T0 + 1, T0 + 50)
    st.flush()
    st.prune(T0 + 3600 + 200)          # past the raw tier only
    assert rows(tmp_path, "SELECT count(*) FROM samples", st)[0][0] == 0
    assert rows(tmp_path, "SELECT count(*) FROM rollup", st)[0][0] == 2
    st.prune(T0 + 86400 + 200)         # past the retention
    assert rows(tmp_path, "SELECT count(*) FROM rollup", st)[0][0] == 0
    assert rows(tmp_path, "SELECT count(*) FROM counters", st)[0][0] == 0
    assert rows(tmp_path, "SELECT count(*) FROM events")[0][0] == 0
    assert [o["wan"] for o in st.outages()] == ["wan1"], "an ongoing outage is never pruned"


def test_close_writes_the_minute_in_progress(tmp_path):
    st, clock = make(tmp_path)
    feed(st, clock, 30)
    st.close()
    st, _ = make(tmp_path)
    assert rows(tmp_path, 'SELECT t, "a|max" FROM rollup', st) == [(T0, 29)]
    assert rows(tmp_path, "SELECT t, c FROM counters", st) == [(T0, 1029)]
    # restarted within that minute: the row is replaced, not duplicated
    feed(st, clock, 40, start=30)
    st.close()
    st, _ = make(tmp_path)
    assert rows(tmp_path, 'SELECT t, "a|max" FROM rollup ORDER BY t', st) == [(T0, 59), (T0 + 60, 69)]


def test_survives_a_crash(tmp_path):
    st, clock = make(tmp_path, flush_seconds=10)
    feed(st, clock, 25)
    st.add_event({"ts": T0, "type": "monitor", "detail": "x"})
    # no close(): the process dies; what was flushed (and every event) is there.
    # (In-process, the DuckDB file stays locked until the connection goes.)
    st.d.close()
    st2, _ = make(tmp_path)
    assert len(st2.history(T0, T0 + 30)[0]) == 21
    assert st2.events()[-1]["detail"] == "x"


def test_readers_while_writing(tmp_path):
    st, clock = make(tmp_path, flush_seconds=1)
    errors, stop = [], threading.Event()

    def reader():
        try:
            while not stop.is_set():
                st.history(T0, T0 + 3000, max_points=100)
                st.events(10)
        except Exception as e:      # e.g. "database is locked"
            errors.append(e)
    ts = [threading.Thread(target=reader) for _ in range(4)]
    for t in ts:
        t.start()
    for i in range(3000):
        clock.t = T0 + i
        st.add(clock.t, {"a": float(i), "b": 1.0}, {"c": i})
        if i % 100 == 0:
            st.add_event({"ts": clock.t, "type": "x"})
    stop.set()
    for t in ts:
        t.join()
    assert errors == []


def test_imports_the_old_event_log(tmp_path):
    old = [
        {"type": "monitor", "ts": T0, "detail": "started"},
        {"type": "outage_start", "ts": T0 + 1, "wan": "wan1", "reason": "link down", "snapshot": {"x": 1}},
        {"type": "outage_end", "ts": T0 + 9, "wan": "wan1", "start": T0 + 1, "duration": 8},
        {"type": "outage_start", "ts": T0 + 20, "wan": "wan2", "reason": "no DHCP lease", "snapshot": None},
    ]
    with open(tmp_path / "events.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in old)
        f.write('{"torn')
    st, _ = make(tmp_path)
    assert st.import_jsonl(str(tmp_path / "events.jsonl")) == 4
    assert not os.path.exists(tmp_path / "events.jsonl") and os.path.exists(tmp_path / "events.jsonl.imported")
    assert [e["type"] for e in st.events()] == [r["type"] for r in old]
    o = {x["wan"]: x for x in st.outages()}
    assert o["wan1"]["duration"] == 8 and o["wan1"]["snapshot"] == {"x": 1} and o["wan2"]["end"] is None


def test_size(tmp_path):
    """the steady growth of the statistics: 1 s graph series (29 values,
    20 of them with +-10% noise, a pessimistic case for compression) and
    minute counter snapshots (190), measured as the space in use between
    hour 2 and hour 6 (the first hours carry fixed overhead). Smooth data
    measured ~3.6 MB a day; this noisy case ~9 MB"""
    st, clock = make(tmp_path)
    rnd = random.Random(1)
    keys = [f"s{k}" for k in range(29)]
    ckeys = [f"c{k}" for k in range(190)]
    cnt = {k: rnd.randrange(10**9) for k in ckeys}

    def used():
        st.flush()
        st.d.execute("CHECKPOINT")
        b, size = st.d.execute("SELECT used_blocks, block_size FROM pragma_database_size()").fetchone()
        return b * size
    mark = {}
    for i in range(6 * 3600):
        clock.t = T0 + i
        if i == 2 * 3600:
            mark[2] = used()
        if i % 60 == 59:
            for k in ckeys[:120]:
                cnt[k] += rnd.randrange(50000 * 60)
        load = 60000 + 140000 * max(0.0, math.sin(math.pi * i / 86400))
        st.add(clock.t, {k: (load * rnd.uniform(0.9, 1.1) if j < 20 else 0.0) for j, k in enumerate(keys)},
               cnt if i % 60 == 59 else None)
    per_day = (used() - mark[2]) * 24 / 4
    st.close()
    print(f"{per_day / 1e6:.1f} MB a day")
    assert per_day < 12e6


def test_collector_history_after_restart(tmp_path):
    cfg = config.parse(f"""
[tls]
enabled = false
[store]
state_dir = "{tmp_path}"
history_seconds = 60
raw_days = 0.004
""")
    clock = Clock(T0)
    stats, wans = FakeStats(), FakeWans(clock)
    col = Collector(cfg, stats, wans, clock=clock)
    for i in range(900):
        clock.t = T0 + i
        wans.tick()
        stats.bump("NatgwModel0", rx_forwarded=100)
        col.step()
    h = col.history_since(60)
    assert len(h) == 61 and h[-1]["hw"] == 100.0, "the last minute from memory"
    h = col.history_since(300)
    assert h[0]["bucket"] == 1 and len(h) >= 290 and h[-1]["max"]["hw"] == 100.0, "5 min: 1 s samples"
    h = col.history_since(890)
    assert h[0]["bucket"] == 60 and 13 <= len(h) <= 16, "beyond the raw tier: minute rollups"
    col.close()
    # restarted: nothing in memory, the database has it all
    col2 = Collector(cfg, stats, wans, clock=clock)
    clock.t += 1
    wans.tick()
    col2.step()
    h = col2.history_since(300)
    assert len(h) >= 290 and h[-2]["hw"] == 100.0
    names = col2.store.counter_names()
    assert "port.NatgwModel0.rx_forwarded" in names and "offload.installed" in names
    pts = col2.counter_history("port.NatgwModel0.rx_forwarded", 900)
    assert len(pts) >= 13 and pts[1][1] - pts[0][1] == 6000, "100 a second, a snapshot a minute"


def test_a_fatal_duckdb_error_reopens_the_database(tmp_path):
    """DuckDB invalidates a connection after a fatal error (e.g. out of
    memory in a checkpoint): the store reopens it and carries on"""
    import duckdb
    st, clock = make(tmp_path)
    feed(st, clock, 15)

    class Failing:
        def __init__(self, d):
            self.d = d

        def execute(self, sql, *a):
            if sql == "COMMIT":
                raise duckdb.FatalException("database has been invalidated")
            return self.d.execute(sql, *a)

        def __getattr__(self, k):
            return getattr(self.d, k)
    st.d = Failing(st.d)
    feed(st, clock, 15, start=15)          # the batch in flight is lost
    assert st.gen == 1 and not isinstance(st.d, Failing)
    feed(st, clock, 30, start=30)
    st.flush()
    h, _ = st.history(T0, T0 + 59)
    assert len(h) >= 40 and h[-1][0] == T0 + 59, "written again after the reopen, and readable"
