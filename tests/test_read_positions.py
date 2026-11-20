"""Read positions (plans/source-read-positions.md): each Run reads each Source up to what that Source had published
when the Run was dispatched (its read position), and the next Run reads that Source's changes from there. A Run's own
freshness is the oldest of its Sources', so a Source can be read past it; before, a Trickle Source was read only up
to the Run's freshness, which hid a windowed Inlet's rows and the rows of the Run that enabled the Sink (item 1), and
a Sink stamped behind a fast merge Source read it as of a point its cleanup had already collected (item 20)."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import duckdb

from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane
from duckstring.engine.core import NEVER
from duckstring.trickle.io import first_row

UTC = timezone.utc


def ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 16, hour, minute, tzinfo=UTC)


def iso(d: dict) -> dict:
    return {k: v.isoformat() for k, v in d.items()}


class Pipeline:
    """Two published Sources, ``fact`` (append, ``id, k, qty``) and ``dim`` (merge, ``k, price``), and a Sink
    ``o`` that joins them with the builder. Each Source Run publishes; each Sink Run reads at given positions."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.dirs = {s: tmp_path / "ponds" / s / "m1" / "data" for s in ("fact", "dim")}
        self.cons = {s: duckdb.connect(str(tmp_path / f"{s}.duckdb")) for s in ("fact", "dim")}
        self.snk = duckdb.connect(str(tmp_path / "o.duckdb"))
        self.snk_dir = tmp_path / "ponds" / "o" / "m1" / "data"
        self.next_id = 1

    def facts(self, f, rows):
        """A ``fact`` Run at ``f`` appending ``[(k, qty), ...]``."""
        con = self.cons["fact"]
        values = []
        for k, qty in rows:
            values.append(f"({self.next_id}, '{k}', {qty})")
            self.next_id += 1
        body = ", ".join(values) if values else "(0, 'x', 0)"
        rel = con.sql(f"SELECT * FROM (VALUES {body}) v(id, k, qty)" + ("" if values else " WHERE false"))
        T.append_table(con, "fact", rel, f, ("id",))
        ParquetDataPlane().export(con, self.dirs["fact"], f=f)

    def dim(self, f, prices: dict, retain_from=None):
        """A ``dim`` Run at ``f`` with the complete state ``{k: price}``."""
        con = self.cons["dim"]
        body = ", ".join(f"('{k}', {p})" for k, p in prices.items())
        T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {body}) v(k, price)"), f, ("k",))
        ParquetDataPlane().export(con, self.dirs["dim"], f=f, retain_from=retain_from)

    def sink(self, f, positions, previous_positions, previous_f, out="append", publish=True):
        p = Pond("o", "1.0.0", self.snk, root=self.root, source_majors={"fact": 1, "dim": 1}, f=f,
                 previous_f=previous_f, read_positions=iso(positions),
                 previous_positions=iso(previous_positions) if previous_positions is not None else None)
        joined = (p.trickle("fact.fact", p=1.0).join(p.trickle("dim.dim", p=1.0), on="k")
                  .select("s0.id, s0.k, s0.qty, s1.price"))
        if out == "append":
            joined.append("o", pk="id")
        else:
            joined.merge("o", pk="id")
        if publish:
            ParquetDataPlane().export(self.snk, self.snk_dir, f=f)
        return p

    def output(self):
        return sorted(T.current_state(self.snk, "o").fetchall())

    def recompute(self, positions):
        """The join over each Source as it stood at its position, from the published files."""
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        dp = ParquetDataPlane()
        fact = dp.read_select(self.dirs["fact"], "fact", as_of=positions["fact"])
        dim = dp.read_select(self.dirs["dim"], "dim", as_of=positions["dim"])
        return sorted(con.sql(f"SELECT f.id, f.k, f.qty, d.price FROM ({fact}) f JOIN ({dim}) d USING (k)").fetchall())


def test_a_sink_reads_the_rows_of_the_source_run_that_enabled_it(tmp_path):
    """The 04:29/04:30 case: ``fact`` has run past ``dim``, so the Sink Run is stamped at ``dim``'s freshness, and
    reads ``fact`` up to its position. The next Run reads only ``fact``'s later rows: nothing twice."""
    pl = Pipeline(tmp_path)
    pl.facts(ts(1), [("a", 1), ("b", 2)])
    pl.dim(ts(1), {"a": 10, "b": 20})
    pos = {"fact": ts(1), "dim": ts(1)}
    pl.sink(ts(1), pos, None, NEVER)
    assert pl.output() == pl.recompute(pos)

    pl.facts(ts(3), [("a", 3)])  # fact runs at 03:00; dim's 02:00 Run changes nothing
    prev, pos = pos, {"fact": ts(3), "dim": ts(1)}
    pl.sink(ts(2), pos, prev, ts(1))  # stamped at dim's 02:00, reading fact to 03:00
    assert [r[0] for r in pl.output()] == [1, 2, 3]  # before: fact's 03:00 row waited for the next Run
    assert pl.output() == pl.recompute(pos)

    pl.facts(ts(4), [("b", 4)])
    pl.dim(ts(4), {"a": 10, "b": 20})
    prev, pos = pos, {"fact": ts(4), "dim": ts(1)}  # dim's 04:00 Run changed nothing: still at 01:00
    pl.sink(ts(3), pos, prev, ts(2))
    assert pl.output() == pl.recompute(pos)  # row 3 once: the window for fact was (03:00, 04:00]


def test_a_windowed_sources_rows_are_seen_by_a_sink_with_another_source(tmp_path):
    """A windowed Inlet's Run is stamped with its window's end, so its rows are stamped in the future. A Sink whose
    other Source is at now reads them at once (item 1: before, an empty join that an append output froze)."""
    pl = Pipeline(tmp_path)
    pl.dim(ts(6), {"a": 10})  # a 6-hour window: fresh until 06:00
    pl.facts(ts(1), [("a", 1)])
    pos = {"fact": ts(1), "dim": ts(6)}
    pl.sink(ts(1), pos, None, NEVER)
    assert pl.output() == [(1, "a", 1, 10)]
    pl.facts(ts(2), [("a", 2)])
    prev, pos = pos, {"fact": ts(2), "dim": ts(6)}
    pl.sink(ts(2), pos, prev, ts(1))
    assert pl.output() == [(1, "a", 1, 10), (2, "a", 2, 10)]  # dim's rows read once


def test_a_sink_stamped_behind_survives_its_source_compacting(tmp_path, monkeypatch):
    """Item 20: a fast merge Source folds and compacts while a Sink stamped at its slower Source's freshness is
    dispatched. The Sink reads the fast Source at its read position, which the Source's bound keeps (its own
    changed_f before the dispatch, the Sink's position after), never as of the Sink's own older stamp."""
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # fold and compact eagerly
    pl = Pipeline(tmp_path)
    pl.facts(ts(0), [("a", 1), ("b", 2), ("c", 3)])
    prices = {"a": 10, "b": 20, "c": 30}
    changed = None
    for h in range(1, 9):  # dim is the fast one here: repriced every Run
        prices = {k: v + 1 if (h + i) % 3 == 0 else v for i, (k, v) in enumerate(prices.items())}
        pl.dim(ts(h), prices, retain_from=changed)  # no Sink in flight: the bound is its own changed_f
        changed = ts(h)
    pos = {"fact": ts(0), "dim": ts(8)}  # the Sink is dispatched: fact (slow) stamps it at 00:00
    at_dispatch = dict(prices)
    for h in range(9, 15):  # dim runs on, folding and compacting, while the Sink Run is in flight
        prices = {k: v + 1 if (h + i) % 2 == 0 else v for i, (k, v) in enumerate(prices.items())}
        pl.dim(ts(h), prices, retain_from=ts(8))  # the bound: the in-flight Sink's position
    assert T.load_sidecar(pl.dirs["dim"])["dim"].get("f_base") > ts(8).isoformat()  # compacted past it
    pl.sink(ts(0), pos, None, NEVER, out="merge")
    # Against what dim held at the position, not the published files (a lost state would read wrong in both).
    assert pl.output() == [(1, "a", 1, at_dispatch["a"]), (2, "b", 2, at_dispatch["b"]), (3, "c", 3, at_dispatch["c"])]


def test_where_a_sinks_changes_start(tmp_path):
    """The previous position per Source: the previous Run's, else ``NEVER`` for a Source it had none for (nothing
    published); ``previous_f`` for a Run with no recorded positions (before read positions); ``NEVER`` on a
    first Run or a Rebuild. A position never falls below the previous one."""
    con = duckdb.connect()

    def pond(previous_f, previous_positions, positions=None):
        return Pond("o", "1.0.0", con, root=tmp_path, f=ts(5), previous_f=previous_f,
                    read_positions=iso(positions or {}),
                    previous_positions=iso(previous_positions) if previous_positions is not None else None)

    p = pond(ts(4), {"a": ts(3)})
    assert p._previous_position("a") == ts(3)
    assert p._previous_position("b") == NEVER
    assert pond(ts(4), None)._previous_position("a") == ts(4)
    assert pond(NEVER, {"a": ts(3)})._previous_position("a") == NEVER
    assert pond(ts(4), {"a": ts(3)}, {"a": ts(6)})._position("a") == ts(6)
    assert pond(ts(4), {"a": ts(7)}, {"a": ts(6)})._position("a") == ts(7)  # a Force re-pinned an earlier Run
    assert pond(ts(4), {"a": ts(3)})._position("a") == ts(5)  # no position: this Run's freshness


def test_sink_runs_at_random_positions_match_a_recompute(tmp_path):
    """Sources change and Sink Runs are dispatched in random orders: each Sink Run is stamped with the oldest of
    its Sources' freshness and reads each at its position. The builder's append, merge and aggregate outputs
    always equal a recompute at the positions, and an append output never holds a row twice."""
    from duckstring import agg

    for seed in range(4):
        rng = random.Random(seed)
        root = tmp_path / str(seed)
        root.mkdir()
        pl = Pipeline(root)
        outs = {o: duckdb.connect(str(root / f"{o}.duckdb")) for o in ("append", "merge", "agg")}
        prices = {k: 10 for k in "abcd"}
        end = {"fact": NEVER, "dim": NEVER}
        changed = {"fact": NEVER, "dim": NEVER}
        clock = ts(1)
        prev_pos, prev_f = None, NEVER
        for step in range(14):
            clock += timedelta(minutes=1)
            for src in rng.sample(["fact", "dim"], k=rng.randint(0, 2)):
                if src == "fact":
                    rows = [(rng.choice("abcd"), rng.randint(1, 9)) for _ in range(rng.randint(0, 3))]
                    pl.facts(clock, rows)
                    if rows:
                        changed["fact"] = clock
                else:
                    if rng.random() < 0.6:
                        prices[rng.choice("abcd")] += 1
                        changed["dim"] = clock
                    pl.dim(clock, dict(prices))
                end[src] = clock
                clock += timedelta(minutes=1)
            if NEVER in end.values():
                continue
            f = min(end.values())
            if f <= prev_f:
                continue
            pos = dict(changed)
            if rng.random() < 0.15:  # a Rebuild: everything from scratch
                for con in outs.values():
                    for t in [r[0] for r in con.sql("SELECT table_name FROM duckdb_tables()").fetchall()]:
                        con.execute(f'DROP TABLE "{t}"')
                    for v in [r[0] for r in con.sql("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]:
                        con.execute(f'DROP VIEW "{v}"')
                prev_pos, prev_f = None, NEVER
            for o, con in outs.items():
                p = Pond("o", "1.0.0", con, root=root, source_majors={"fact": 1, "dim": 1}, f=f, previous_f=prev_f,
                         read_positions=iso(pos), previous_positions=iso(prev_pos) if prev_pos else None)
                j = p.trickle("fact.fact", p=1.0).join(p.trickle("dim.dim", p=1.0), on="k")
                if o == "append":
                    j.select("s0.id, s0.k, s0.qty, s1.price").append("o", pk="id")
                elif o == "merge":
                    j.select("s0.id, s0.k, s0.qty, s1.price").merge("o", pk="id")
                else:
                    j.aggregate(by="k", units=agg.sum("qty")).merge("o")
            want = pl.recompute(pos)
            for o in ("append", "merge"):
                got = sorted(T.current_state(outs[o], "o").fetchall())
                if o == "append":  # an append output freezes the price it saw; check the rows, once each
                    assert [r[:3] for r in got] == [r[:3] for r in want], (seed, step, o)
                else:
                    assert got == want, (seed, step, o)
            totals: dict = {}
            for _id, k, qty, _price in want:
                totals[k] = totals.get(k, 0) + qty
            got = {k: int(u) for k, u in T.current_state(outs["agg"], "o").fetchall()}
            assert got == totals, (seed, step, "agg")
            prev_pos, prev_f = pos, f


# ─── the Catchment: the job and the run history ─────────────────────────────────


def _driver(tmp_path):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher
    from duckstring.catchment.routes.deploy import _register

    db = connect(tmp_path / "duck.db")
    migrate(db)
    cfg = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "pond"}
    _register(db, "sales", "1.0.0", "pond", "ponds/sales/1.0.0", cfg,
              [{"func": "f", "name": "agg", "parents": []}])
    _register(db, "rpt", "1.0.0", "outlet", "ponds/rpt/1.0.0",
              {**cfg, "kind": "outlet", "sources": {"sales": "1.0.0"}},
              [{"func": "f", "name": "out", "parents": []}])
    return Driver(db, tmp_path, "http://x", NoopLauncher())


def _begin_run(driver, pond):
    return next(j for j in driver.jobs[pond] if j["kind"] == "begin_run")


def test_the_job_carries_the_previous_runs_positions(tmp_path):
    driver = _driver(tmp_path)
    with driver.lock:
        ss = driver.state.pond_states["sales@1"]
        ss.start_f = ss.end_f = ss.changed_f = ss.persisted_f = ts(1)
        driver._dispatch_begin_run("rpt@1", ts(1), ts(1))
    job = _begin_run(driver, "rpt@1")
    assert job["read_positions"] == {"sales": ts(1).isoformat()}
    assert job["previous_positions"] is None  # a first Run: from previous_f (NEVER)

    driver.db.execute("UPDATE pond_run SET status = 'success'")
    driver.db.commit()
    driver.jobs["rpt@1"] = []
    with driver.lock:
        ss = driver.state.pond_states["sales@1"]
        ss.start_f = ss.end_f = ss.changed_f = ss.persisted_f = ts(3)
        driver._dispatch_begin_run("rpt@1", ts(2), ts(3))
    job = _begin_run(driver, "rpt@1")
    assert job["read_positions"] == {"sales": ts(3).isoformat()}
    assert job["previous_positions"] == {"sales": ts(1).isoformat()}

    # A pass reads nothing: the Run after it goes on from the positions of the Run before it.
    driver.db.execute("UPDATE pond_run SET status = 'success'")
    driver.db.commit()
    with driver.lock:
        driver._record_pass("rpt@1", ts(4), ts(4))
    row = first_row(driver.db.execute("SELECT read_positions FROM pond_run WHERE f = ?", (ts(4).isoformat(),)))
    assert json.loads(row[0]) == {"sales": ts(3).isoformat()}
    assert driver._previous_positions("rpt@1", ts(5)) == {"sales": ts(3).isoformat()}

    # A Rebuild reads everything.
    driver.jobs["rpt@1"] = []
    with driver.lock:
        driver._dispatch_begin_run("rpt@1", ts(5), ts(5), rebuild=True)
    assert _begin_run(driver, "rpt@1")["previous_positions"] is None


def test_runs_recorded_before_read_positions_read_on_from_previous_f(tmp_path):
    """Migration 038 clears the positions of Runs recorded before it: those Runs read a Trickle Source only up to
    their own freshness, so the next Run must read on from there (previous_f), not from their pins."""
    driver = _driver(tmp_path)
    vid = driver.meta["rpt@1"]["version_id"]
    driver.db.execute("INSERT INTO pond_run (pond_version_id, f, started_at, status, read_positions) "
                      "VALUES (?, ?, ?, 'success', NULL)", (vid, ts(1).isoformat(), ts(1).isoformat()))
    driver.db.commit()
    assert driver._previous_positions("rpt@1", ts(2)) is None


# ─── the Duck: pipelined Runs and restarts ──────────────────────────────────────


def _core(tmp_path, con=None):
    from duckstring.duck.core import DuckCore
    from duckstring.engine import pond as ledger

    con = con or ledger.connect(tmp_path / "pond.db")
    return DuckCore("o", con, {"r": []}), con


def test_a_ripple_going_on_from_its_own_run_reads_from_that_runs_positions(tmp_path):
    """Pond Runs pipeline: a Run dispatched while an earlier one is in flight is sent the same previous Run. A
    Ripple that has completed the earlier Run goes on from it, freshness and positions alike, and so does a
    restarted Duck, from its ledger."""
    core, con = _core(tmp_path)
    p1 = {"src": ts(1).isoformat()}
    core.begin_run(ts(1), ts(1), previous_f=NEVER, read_positions=p1, previous_positions=None)
    core.begin_run(ts(2), ts(2), previous_f=NEVER, read_positions={"src": ts(2).isoformat()},
                   previous_positions=None)
    assert core.previous_positions_for(ts(2), "r") is None  # before r completes Run 1: the job's
    core.ripple_completed("r", ts(2))
    assert core.previous_f_for(ts(2), "r") == ts(1)
    assert core.previous_positions_for(ts(2), "r") == p1

    restarted, _ = _core(tmp_path, con)
    restarted.begin_run(ts(2), ts(3), previous_f=NEVER, read_positions={"src": ts(2).isoformat()},
                        previous_positions=None)
    assert restarted.previous_f_for(ts(2), "r") == ts(1)
    assert restarted.previous_positions_for(ts(2), "r") == p1


def test_a_redispatched_run_keeps_its_positions(tmp_path):
    core, _ = _core(tmp_path)
    core.begin_run(ts(1), ts(1), read_positions={"src": ts(1).isoformat()}, previous_positions={"src": "x"})
    core.begin_run(ts(1), ts(2), read_positions={"src": ts(2).isoformat()}, previous_positions=None)
    core.ripple_completed("r", ts(2))
    assert core._ripple_positions["r"] == {"src": ts(1).isoformat()}

