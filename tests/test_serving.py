"""Data serving increment 1 (plans/data-serving.md): the exposure model (declared serviceable +
operational override + reset-on-new-major + served-major/promote) and the sandboxed serving core."""

from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb
import pytest
from trickle.io import first_row

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.registry import pond_data_dir
from duckstring.catchment.routes.deploy import _register
from duckstring.catchment.serving import serving_query
from duckstring.keys import pond_key

pytestmark = pytest.mark.timeout(10)

_RIPPLES = [{"func": "f", "name": "r", "parents": []}]


def _cfg(serve_tables=None, **extra):
    cfg = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet",
           "serve_tables": serve_tables or []}
    cfg.update(extra)
    return cfg


def _pv_id(db, name, version):
    return first_row(db.execute(
        "SELECT pv.id FROM pond_version pv JOIN pond_name pn ON pn.id = pv.pond_name_id "
        "WHERE pn.name = ? AND pv.version = ?", (name, version)))[0]


def _add_output_tables(db, pv_id, tables):
    for t in tables:
        db.execute('INSERT OR IGNORE INTO pond_version_schema (pond_version_id, "table", "column", type) '
                   "VALUES (?, ?, 'id', 'INTEGER')", (pv_id, t))
    db.commit()


def _driver(tmp_path, launcher=None):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    return db, lambda: Driver(db, tmp_path, "http://x", launcher or NoopLauncher())


# ─── the exposure model ─────────────────────────────────────────────────────────


def test_declared_serviceable_and_override(tmp_path):
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["revenue"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["revenue", "secret_internal"])
    d = mk()
    key = pond_key("sales", 1)

    assert d.serviceable(key) == {"revenue"}  # declared; the intermediate stays hidden
    detail = {r["table"]: r for r in d.serve_detail(key)}
    assert detail["revenue"]["exposed"] and detail["revenue"]["source"] == "declared"
    assert not detail["secret_internal"]["exposed"] and detail["secret_internal"]["source"] == "hidden"

    d.set_exposed(key, "secret_internal", True)   # eye on a non-declared table
    assert d.serviceable(key) == {"revenue", "secret_internal"}
    d.set_exposed(key, "revenue", False)          # eye off a declared table
    assert d.serviceable(key) == {"secret_internal"}
    d.set_exposed(key, "revenue", None)           # clear → back to declared
    assert d.serviceable(key) == {"secret_internal", "revenue"}


def test_override_resets_on_new_major(tmp_path):
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["revenue"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["revenue"])
    d = mk()
    d.set_exposed(pond_key("sales", 1), "revenue", False)  # hide on v1
    assert d.serviceable(pond_key("sales", 1)) == set()

    # A NEW major line starts fresh from pond.toml (no override carry-over).
    _register(db, "sales", "2.0.0", "outlet", "ponds/sales/2.0.0", _cfg(serve_tables=["revenue"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "2.0.0"), ["revenue"])
    d = mk()
    assert d.serviceable(pond_key("sales", 2)) == {"revenue"}   # declared default, not v1's hide
    assert d.serviceable(pond_key("sales", 1)) == set()          # v1's override persists on its line


def test_served_major_and_promote(tmp_path):
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["revenue"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["revenue"])
    d = mk()
    assert d.served_major("sales") == 1  # first deploy auto-serves

    # Deploy v2 that does NOT publish `revenue` → promote refused (would 404 a live query).
    _register(db, "sales", "2.0.0", "outlet", "ponds/sales/2.0.0", _cfg(serve_tables=["revenue_v2"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "2.0.0"), ["revenue_v2"])
    d = mk()
    assert d.served_major("sales") == 1  # a new major does NOT auto-advance the served pointer
    with pytest.raises(ValueError, match="doesn't publish"):
        d.promote("sales", 2)

    # A v2 that DOES publish the served table promotes cleanly.
    _add_output_tables(db, _pv_id(db, "sales", "2.0.0"), ["revenue"])
    d = mk()
    d.promote("sales", 2)
    assert d.served_major("sales") == 2
    with pytest.raises(ValueError, match="not deployed"):
        d.promote("sales", 9)


# ─── the sandboxed serving core ──────────────────────────────────────────────────


def _publish(tmp_path, name, major, table, n):
    """Write a plain-overwrite Parquet table to the pond's data dir (what the parquet plane reads)."""
    data_dir = pond_data_dir(tmp_path, name, major, None)
    data_dir.mkdir()
    con = duckdb.connect()
    with data_dir.copy_to(f"{table}.parquet") as uri:
        con.execute(f"COPY (SELECT range AS id FROM range({n})) TO '{uri}' (FORMAT PARQUET)")
    con.close()


def test_serving_core_sandboxed(tmp_path, monkeypatch):
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["revenue"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["revenue", "secret_internal"])
    _publish(tmp_path, "sales", 1, "revenue", 7)
    _publish(tmp_path, "sales", 1, "secret_internal", 3)
    d = mk()

    # The served (bare) schema serves the serviceable table.
    res = serving_query(d, "SELECT count(*) AS n FROM sales.revenue")
    assert res["columns"] == ["n"] and res["rows"] == [(7,)]
    # The explicit _v1 form works too (pinning).
    assert serving_query(d, "SELECT count(*) FROM sales_v1.revenue")["rows"] == [(7,)]

    # A non-serviceable table is not in the surface at all.
    with pytest.raises(duckdb.Error):
        serving_query(d, "SELECT * FROM sales.secret_internal")

    # The sandbox blocks escaping the allowlist via external file access.
    with pytest.raises(duckdb.Error):
        serving_query(d, f"SELECT * FROM read_parquet('{tmp_path}/ponds/sales/m1/data/secret_internal.parquet')")

    # And specifically NOT from the system temp directory. DuckDB exempts temp_directory from the
    # external-access lock (it must be able to spill), so pointing that at the shared system temp used to
    # hand a read user every Parquet under /tmp. Only Linux CI caught it — on macOS the path above does
    # not sit inside gettempdir(), so the check above passed while the hole was wide open.
    probe = Path(tempfile.mkdtemp()) / "not_yours.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1 AS x) TO '{probe}' (FORMAT PARQUET)")
    con.close()
    with pytest.raises(duckdb.Error):
        serving_query(d, f"SELECT * FROM read_parquet('{probe}')")


def test_full_connection_sees_all_tables(tmp_path, monkeypatch):
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["revenue"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["revenue", "secret_internal"])
    _publish(tmp_path, "sales", 1, "revenue", 7)
    _publish(tmp_path, "sales", 1, "secret_internal", 3)
    d = mk()
    # An ops (full) connection sees every output table, not just the serviceable ones.
    assert serving_query(d, "SELECT count(*) FROM sales.secret_internal", sandboxed=False)["rows"] == [(3,)]


def _publish_merge(tmp_path, name, major, table, runs, *, hold_last=False):
    """Publish a merge Trickle run by run, as a Duck does (one export per Run). ``hold_last`` publishes the last
    Run with compaction out of reach, so its change stays above the base whatever size the Parquet files are."""
    import os
    from datetime import datetime

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane

    data_dir = pond_data_dir(tmp_path, name, major, None)
    data_dir.mkdir()
    reg = tmp_path / f"{name}_registry.duckdb"
    con = duckdb.connect(str(reg))
    con.execute("SET TimeZone='UTC'")
    for i, (f_iso, rows) in enumerate(runs):
        # One literal statement: row-by-row inserts (and, on DuckDB 1.5, list parameters) are slow.
        values = ",".join(f"({k},{v})" for k, v in rows)
        con.execute("CREATE OR REPLACE TEMP TABLE _s AS SELECT id::BIGINT AS id, v::BIGINT AS v "
                    f"FROM (VALUES {values}) t(id, v)")
        f = datetime.fromisoformat(f_iso)
        held = hold_last and i == len(runs) - 1
        before = os.environ.get("DUCKSTRING_COMPACT_THRESHOLD")
        if held:
            os.environ["DUCKSTRING_COMPACT_THRESHOLD"] = str(1 << 40)
        try:
            trickle_io.merge_table(con, table, con.sql("SELECT * FROM _s"), f, ("id",))
            ParquetDataPlane().export(con, data_dir, f=f)
        finally:
            if held:
                if before is None:
                    os.environ.pop("DUCKSTRING_COMPACT_THRESHOLD", None)
                else:
                    os.environ["DUCKSTRING_COMPACT_THRESHOLD"] = before
    con.close()


def test_a_merge_table_is_consolidated_once_per_snapshot(tmp_path, monkeypatch):
    """Over the operator's connection, a query reconsolidated a merge table's change log twice, and the data viewer
    sends a count and every page. Now the first query that reads it writes one consolidation file, later queries
    and connections rebuilt for other publishes reuse it, and a new publish of the table replaces it."""
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["t"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["t"])
    _publish_merge(tmp_path, "sales", 1, "t", [
        ("2026-01-01T00:00:00+00:00", [(1, 10), (2, 20), (3, 30)]),
        ("2026-01-02T00:00:00+00:00", [(1, 11), (3, 30), (4, 40)]),
    ])
    d = mk()
    cache = tmp_path / ".serving-cache"
    q = "SELECT id, v FROM sales.t ORDER BY id"
    assert not cache.exists() or not any(cache.iterdir())
    assert serving_query(d, "SELECT 1", sandboxed=False)["rows"] == [(1,)]  # a query that reads no merge table
    assert not cache.exists() or not any(cache.iterdir())
    assert serving_query(d, q, sandboxed=False)["rows"] == [(1, 11), (3, 30), (4, 40)]
    first = sorted(cache.iterdir())
    assert len(first) == 2  # the images and the retracted keys
    assert serving_query(d, "SELECT count(*) FROM sales_v1.t", sandboxed=False)["rows"] == [(3,)]
    d.data_version += 1  # another Pond published: the connection is rebuilt
    assert serving_query(d, q, sandboxed=False)["rows"] == [(1, 11), (3, 30), (4, 40)]
    assert sorted(cache.iterdir()) == first

    _publish_merge(tmp_path, "sales", 1, "t", [("2026-01-03T00:00:00+00:00", [(1, 12), (4, 40)])])
    d.data_version += 1
    assert serving_query(d, q, sandboxed=False)["rows"] == [(1, 12), (4, 40)]
    assert len(list(cache.iterdir())) == 2 and not set(cache.iterdir()) & set(first)  # the old snapshot's are gone

    # A read user's sandbox materialises from the same file, and can't read the cache directory itself.
    assert serving_query(d, q)["rows"] == [(1, 12), (4, 40)]
    with pytest.raises(duckdb.Error):
        serving_query(d, f"SELECT * FROM read_parquet('{cache}/*.parquet')")


@pytest.mark.timeout(60)
def test_a_consolidated_merge_table_reads_as_its_reconstruction(tmp_path, monkeypatch):
    """Random updates, deletes, re-adds and writes back to an earlier value, through folds and compactions: the
    current state the serving surface reads from its consolidation files is the data plane's reconstruction."""
    import random

    from duckstring.dataplane import ParquetDataPlane

    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1024")
    rng = random.Random(3)
    state = {i: rng.randint(0, 3) for i in range(1500)}
    runs = []
    for run in range(30):
        for k in rng.sample(range(1520), 15):
            if k in state and rng.random() < 0.3:
                del state[k]
            else:
                state[k] = rng.randint(0, 3)  # often the value it had: an update that changes nothing
        runs.append((f"2026-01-{1 + run // 24:02d}T{run % 24:02d}:00:00+00:00", sorted(state.items())))
    k = min(state)  # a last Run updating a key its base holds, left above the base: the retraction path is read
    state[k] = (state[k] + 1) % 4
    runs.append(("2026-01-02T06:00:00+00:00", sorted(state.items())))
    db, mk = _driver(tmp_path)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(serve_tables=["t"]), _RIPPLES)
    _add_output_tables(db, _pv_id(db, "sales", "1.0.0"), ["t"])
    _publish_merge(tmp_path, "sales", 1, "t", runs, hold_last=True)
    d = mk()
    data_dir = pond_data_dir(tmp_path, "sales", 1, None)
    expected = sorted(duckdb.sql(ParquetDataPlane().read_select(data_dir, "t")).fetchall())
    assert sorted((k, v) for k, v, *_ in expected) == sorted(state.items())
    for sandboxed in (False, True):
        got = serving_query(d, "SELECT * FROM sales.t", sandboxed=sandboxed)
        assert sorted(got["rows"]) == expected
    retracted = duckdb.sql(f"SELECT count(*) FROM '{tmp_path}/.serving-cache/*.retract.parquet'").fetchall()[0][0]
    assert retracted > 0  # the anti-join path, not only new keys
