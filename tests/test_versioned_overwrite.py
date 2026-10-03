"""Versioned overwrite tables (plans/versioned-overwrite.md): each run publishes a plain table as a new
immutable ``{table}__v/{f}.parquet``, a Sink run reads the version its Source had published when the run
started (its pin), and retention keeps a superseded version only while a reader may still need it."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring.dataplane import (
    ParquetDataPlane,
    persist_tree,
    prune_versions,
    retained_versions,
)
from duckstring.storage import LocalStorage
from duckstring.trickle.io import load_sidecar, part_name

F1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
F2 = F1 + timedelta(hours=1)
F3 = F1 + timedelta(hours=2)


def _src_dir(root):
    return root / "ponds" / "src" / "m1" / "data"


def _publish(root, f, value, retain_from=None, **kw):
    """One Source run at ``f``: two plain tables whose rows carry ``value``."""
    from duckstring.dataplane import KEEP_ALL

    con = duckdb.connect()
    con.execute(f"CREATE TABLE a AS SELECT {value} AS v")
    con.execute(f"CREATE TABLE b AS SELECT {value} * 10 AS w")
    ParquetDataPlane().export(con, _src_dir(root), f=f,
                              retain_from=KEEP_ALL if retain_from == "keep" else retain_from, **kw)
    con.close()


def _sink(root, pin, f=None, previous_f=None):
    """A Sink run's Pond handle, pinned to the Source's published freshness ``pin``."""
    from duckstring.core import Pond

    return Pond(name="snk", version="1.0.0", con=duckdb.connect(), root=root, source_majors={"src": 1},
                source_f={"src": pin.isoformat()} if pin else {}, f=f or pin, previous_f=previous_f)


def _versions(root, table="a"):
    return sorted(p.name for p in (_src_dir(root) / f"{table}__v").glob("*.parquet"))


@pytest.fixture(autouse=True)
def _parquet_plane(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "parquet")


# ─── publish + pinned read ───────────────────────────────────────────────────────


def test_each_run_publishes_a_new_version(tmp_path):
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    assert _versions(tmp_path) == [part_name(F1), part_name(F2)]
    assert not (_src_dir(tmp_path) / "a.parquet").exists()
    dp = ParquetDataPlane()
    assert dp.list_tables(_src_dir(tmp_path)) == ["a", "b"]
    # Readers with no pin (the data viewer, serving, a Draw) see the newest.
    assert duckdb.sql(dp.read_select(_src_dir(tmp_path), "a")).fetchall() == [(2,)]
    assert dp.table_path(_src_dir(tmp_path), "a").name == part_name(F2)


def test_a_sink_run_keeps_its_version_while_the_source_republishes(tmp_path):
    """Pinned at F1, every Ripple of the run reads F1's version of every Source table, even after the
    Source publishes F2 mid-run."""
    _publish(tmp_path, F1, 1, retain_from="keep")
    first = _sink(tmp_path, F1)
    assert first.read_table("src.a").fetchall() == [(1,)]

    _publish(tmp_path, F2, 2, retain_from=F1)  # the Source republishes; the Sink's pin is retained

    second = _sink(tmp_path, F1)  # a later Ripple of the same run
    assert second.read_table("src.a").fetchall() == [(1,)]
    assert second.read_table("src.b").fetchall() == [(10,)]
    assert second.count_table("src.a") == 1


def test_concurrent_sink_runs_each_read_their_own_version(tmp_path):
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    assert _sink(tmp_path, F1).read_table("src.a").fetchall() == [(1,)]
    assert _sink(tmp_path, F2).read_table("src.a").fetchall() == [(2,)]


def test_the_pin_is_the_sources_published_freshness_not_the_sinks(tmp_path):
    """A Sink's freshness is the minimum over its Sources, so it can be older than this Source's newest
    publish. The pin (what the Source had published when the run started) still reads that newest
    version rather than an older one."""
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F3, 3, retain_from="keep")
    sink = _sink(tmp_path, F3, f=F2)
    assert sink.read_table("src.a").fetchall() == [(3,)]


def test_without_a_pin_a_read_falls_back_to_the_runs_freshness(tmp_path):
    """A puddle run has no job, so no pin: the run's own freshness selects the version."""
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F3, 3, retain_from="keep")
    assert _sink(tmp_path, None, f=F2).read_table("src.a").fetchall() == [(1,)]


def test_a_pruned_pin_reads_the_newest_and_warns(tmp_path, caplog):
    _publish(tmp_path, F2, 2, retain_from="keep")
    with caplog.at_level(logging.WARNING, logger="duckstring.dataplane"):
        assert _sink(tmp_path, F1).read_table("src.a").fetchall() == [(2,)]
    assert "no published version" in caplog.text


def test_read_delta_judges_change_by_the_pinned_version(tmp_path):
    """An overwrite Source whose newest publish is past the run's pin has, for this run, not changed."""
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    stale = _sink(tmp_path, F1, previous_f=F1).read_delta("src.a")
    assert not stale.is_full and stale.is_empty()
    fresh = _sink(tmp_path, F2, previous_f=F1).read_delta("src.a")
    assert fresh.is_full and fresh.upserts.fetchall() == [(2,)]


# ─── retention ───────────────────────────────────────────────────────────────────


def test_retained_versions_rule():
    names = [part_name(f) for f in (F1, F2, F3)]
    assert retained_versions(names, None) == {names[2]}  # no reader: only the newest
    assert retained_versions(names, F1) == set(names)  # pinned at F1: F1 and everything newer
    # A bound between versions keeps the version it resolves to (F1) and everything newer.
    assert retained_versions(names, F1 + timedelta(minutes=30)) == set(names)
    assert retained_versions(names, F2) == {names[1], names[2]}
    assert retained_versions(names, F3 + timedelta(hours=1)) == {names[2]}


def test_publish_prunes_to_retain_from_and_records_the_floor(tmp_path):
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    _publish(tmp_path, F3, 3, retain_from=F2)
    assert _versions(tmp_path) == [part_name(F2), part_name(F3)]
    assert _versions(tmp_path, "b") == [part_name(F2), part_name(F3)]
    assert load_sidecar(_src_dir(tmp_path))["a"]["v_floor"] == F2.isoformat()

    prune_versions(_src_dir(tmp_path), None)  # nothing in flight any more (the shutdown prune)
    assert _versions(tmp_path) == [part_name(F3)]
    assert load_sidecar(_src_dir(tmp_path))["a"]["v_floor"] == F3.isoformat()


def test_an_overlapping_runs_version_is_not_pruned(tmp_path):
    """Run X (dispatched when the Source had published F1) finishes after an overlapping run published F2.
    Its retain_from is at most F1, so F2, which a Sink may have been dispatched onto meanwhile, survives."""
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")  # the overlapping run
    _publish(tmp_path, F3, 3, retain_from=F1)       # run X
    assert _versions(tmp_path) == [part_name(F1), part_name(F2), part_name(F3)]


def test_publish_plan_carries_the_floor_forward(tmp_path):
    """The sidecar is rewritten at the start of every publish; the retention watermark must survive it so
    a Persist that reads the sidecar mid-publish still has it."""
    from duckstring.dataplane import publish_plan

    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from=None)
    con = duckdb.connect()
    con.execute("CREATE TABLE a AS SELECT 9 AS v")
    publish_plan(con, _src_dir(tmp_path), F3)
    assert load_sidecar(_src_dir(tmp_path))["a"]["v_floor"] == F2.isoformat()


# ─── other readers and writers ───────────────────────────────────────────────────


def test_persist_propagates_pruned_versions_by_the_floor(tmp_path):
    """Persist never prunes because a file is absent locally; the sidecar's ``v_floor`` is the signal."""
    local, durable = _src_dir(tmp_path), tmp_path / "durable"
    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    persist_tree(LocalStorage(local), LocalStorage(durable))
    assert sorted(p.name for p in (durable / "a__v").glob("*.parquet")) == [part_name(F1), part_name(F2)]

    _publish(tmp_path, F3, 3, retain_from=None)
    persist_tree(LocalStorage(local), LocalStorage(durable))
    assert sorted(p.name for p in (durable / "a__v").glob("*.parquet")) == [part_name(F3)]
    assert ParquetDataPlane().list_tables(LocalStorage(durable)) == ["a", "b"]


def test_draw_ships_the_newest_version_only(tmp_path):
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app

    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    from duckstring.catchment.routes.deploy import _register

    app = create_app(tmp_path)
    with TestClient(app) as client:
        _register(app.state.db, "src", "1.0.0", "inlet", "ponds/src/1.0.0",
                  {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"},
                  [{"func": "f", "name": "a", "parents": []}])
        resp = client.get("/api/draw/src/1")
        assert resp.status_code == 200, resp.text
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert f"a__v/{part_name(F2)}" in names and f"b__v/{part_name(F2)}" in names
    assert not any(part_name(F1) in n for n in names)


def test_spout_mirror_holds_only_the_newest_version(tmp_path):
    from duckstring.egress.base import get_egress

    _publish(tmp_path, F1, 1, retain_from="keep")
    dest = tmp_path / "mirror"
    drv = get_egress(f"file://{dest}")
    src = LocalStorage(_src_dir(tmp_path))
    drv.mirror(src, "a", load_sidecar(src)["a"], f=F1)
    _publish(tmp_path, F2, 2, retain_from="keep")
    drv.mirror(src, "a", load_sidecar(src)["a"], f=F2)
    assert sorted(p.name for p in (dest / "a__v").glob("*.parquet")) == [part_name(F2)]


def test_hydrate_registry_loads_the_newest_version(tmp_path):
    from duckstring.dataplane import hydrate_registry

    _publish(tmp_path, F1, 1, retain_from="keep")
    _publish(tmp_path, F2, 2, retain_from="keep")
    con = duckdb.connect()
    assert sorted(hydrate_registry(con, _src_dir(tmp_path))) == ["a", "b"]
    assert con.sql("SELECT v FROM a").fetchall() == [(2,)]


def test_unpublish_removes_the_versions(tmp_path):
    from duckstring.dataplane import unpublish_table

    _publish(tmp_path, F1, 1, retain_from="keep")
    unpublish_table(_src_dir(tmp_path), "a")
    assert not (_src_dir(tmp_path) / "a__v").exists()
    assert ParquetDataPlane().list_tables(_src_dir(tmp_path)) == ["b"]


def test_a_single_file_table_is_still_read(tmp_path):
    """Puddle snapshots (``puddles/ponds/{source}/data/{table}.parquet``) keep the single-file layout."""
    d = _src_dir(tmp_path)
    d.mkdir(parents=True)
    duckdb.sql("SELECT 7 AS v").write_parquet(str(d / "a.parquet"))
    dp = ParquetDataPlane()
    assert dp.list_tables(d) == ["a"]
    assert duckdb.sql(dp.read_select(d, "a", as_of=F1)).fetchall() == [(7,)]


# ─── the Duck's per-run inputs ───────────────────────────────────────────────────


def test_the_executor_keeps_inputs_per_run(tmp_path):
    """Pipelined Runs don't share pins; a re-dispatch of a Run in flight keeps its first pins (unless it
    is a Force), and the publish consumes that Run's retain_from."""
    from duckstring.dataplane import KEEP_ALL
    from duckstring.duck.executor import RippleExecutor

    ex = RippleExecutor("src", 1, "1.0.0", "ponds/src/1.0.0", tmp_path)
    try:
        ex.begin_run_inputs(F1, {"up": F1.isoformat()}, F1)
        ex.begin_run_inputs(F2, {"up": F2.isoformat()}, None)
        ex.begin_run_inputs(F1, {"up": F3.isoformat()}, F3)  # a re-dispatch: the first pins stand
        assert ex.source_f_for(F1) == {"up": F1.isoformat()}
        assert ex.source_f_for(F2) == {"up": F2.isoformat()}
        ex.begin_run_inputs(F1, {"up": F3.isoformat()}, F3, force=True)
        assert ex.source_f_for(F1) == {"up": F3.isoformat()}
        assert ex.take_retain_from(F2) is None
        assert ex.take_retain_from(F2) is KEEP_ALL  # consumed
        ex.begin_run_inputs(F3, {}, KEEP_ALL)
        assert ex.take_retain_from(F3) is KEEP_ALL

        cur = ex._cursor()
        cur.execute("CREATE TABLE event AS SELECT 1 AS id")
        cur.close()
        ex.begin_run_inputs(F2, {}, None)
        ex.export(F1)
        ex.export(F2)  # retain_from=None: only the newest remains
        assert sorted(p.name for p in (ex.own_data_dir.root / "event__v").glob("*.parquet")) == [part_name(F2)]
    finally:
        ex.shutdown()


def test_the_duck_reads_retain_from_off_the_job():
    from duckstring.dataplane import KEEP_ALL
    from duckstring.duck.__main__ import _retain_from

    assert _retain_from({}) is KEEP_ALL
    assert _retain_from({"retain_from": None}) is None
    assert _retain_from({"retain_from": F1.isoformat()}) == F1


# ─── the Catchment: pins and retain_from ─────────────────────────────────────────


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


def test_a_sink_runs_pins_are_recorded_and_bound_its_sources_retention(tmp_path):
    driver = _driver(tmp_path)
    assert driver._retain_from("sales@1") is None  # nothing published, nothing in flight

    with driver.lock:
        ss = driver.state.pond_states["sales@1"]
        ss.start_f = ss.end_f = ss.changed_f = ss.persisted_f = F1
    driver.tap("rpt@1")
    job = next(j for j in driver.jobs["rpt@1"] if j["kind"] == "begin_run")
    assert job["source_f"] == {"sales": F1.isoformat()}
    row = driver.db.execute("SELECT f, source_pins FROM pond_run WHERE status = 'running'").fetchone()
    assert json.loads(row[1]) == {"sales": F1.isoformat()}

    # The Source publishes F2 while the Sink runs: its versions must reach back to the Sink's pin.
    with driver.lock:
        ss = driver.state.pond_states["sales@1"]  # the engine replaces its state object on each tick
        ss.start_f = ss.end_f = ss.changed_f = F2
    assert driver._retain_from("sales@1") == F1.isoformat()

    # A re-dispatch of the Sink's Run (a Catchment restart) re-sends the pins it started with.
    driver.jobs["rpt@1"] = []
    with driver.lock:
        driver._dispatch_begin_run("rpt@1", datetime.fromisoformat(row[0]), F3)
    job = next(j for j in driver.jobs["rpt@1"] if j["kind"] == "begin_run")
    assert job["source_f"] == {"sales": F1.isoformat()}
    # The Source's own begin_run job carries the bound.
    assert "retain_from" in job

    # The Sink's Run finishes: only the Source's own published version bounds retention now.
    driver.db.execute("UPDATE pond_run SET status = 'success'")
    driver.db.commit()
    assert driver._retain_from("sales@1") == F2.isoformat()


def test_a_stranded_running_row_does_not_hold_versions(tmp_path):
    driver = _driver(tmp_path)
    with driver.lock:
        ss = driver.state.pond_states["sales@1"]
        ss.start_f = ss.end_f = ss.changed_f = ss.persisted_f = F1
    driver.tap("rpt@1")
    with driver.lock:
        driver.state.pond_states["sales@1"].changed_f = F3
        rs = driver.state.pond_states["rpt@1"]
        rs.end_f = F2  # the Sink advanced past its Run at F1 without it closing
    assert driver._retain_from("sales@1") == F3.isoformat()

