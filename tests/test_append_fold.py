"""An append table's history was kept whole in the registry as well as published (attention FINDINGS 9: 3.7 GB in
`wa_views`' registry beside 3.2 GB published). By compaction's rule, a publish now moves its older runs out of the
registry: the table's name is a view over the published files for those runs and the registry rows of the rest, so
everything that reads the table (conflict checks, a recompute, Ripple SQL) still reads its whole history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.dataplane import ParquetDataPlane, hydrate_registry

pytestmark = pytest.mark.timeout(60)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts(i):
    return T0 + timedelta(hours=i)


def _con():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def _write(con, data_dir, i, **kw):
    rel = con.sql(f"SELECT {i} * 1000 + range AS id, repeat('x', 40) AS pad FROM range(200)")
    T.append_table(con, "ev", rel, _ts(i), ("id",), **kw)
    ParquetDataPlane().export(con, data_dir, f=_ts(i))


def _local_runs(con):
    return sorted(r[0] for r in con.execute(f'SELECT DISTINCT "{T.F_COL}" FROM {T.hot_name("ev")}').fetchall())


def test_a_publish_moves_older_runs_out_of_the_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "8192")
    con, data_dir = _con(), tmp_path / "data"
    for i in range(30):
        _write(con, data_dir, i)
    local = _local_runs(con)
    assert 1 <= len(local) < 15 and local[-1] == _ts(29)  # a recent tail, never the whole history
    assert con.execute("SELECT count(*), count(DISTINCT id) FROM ev").fetchall() == [(6000, 6000)]
    published = duckdb.sql(f"SELECT count(*) FROM ({ParquetDataPlane().read_select(data_dir, 'ev')})").fetchall()
    assert published == [(6000,)]
    # A conflict check reads the whole history: a key moved out is still there.
    n = T.append_table(con, "ev", con.sql("SELECT 5 AS id, 'y' AS pad"), _ts(30), ("id",), on_conflict="keep_prior")
    assert n is False and con.execute("SELECT pad FROM ev WHERE id = 5").fetchall() == [("x" * 40,)]
    # So does Ripple SQL, and the table's new state is published as usual.
    T.append_table(con, "ev", con.sql("SELECT 99999 AS id, 'z' AS pad"), _ts(31), ("id",))
    ParquetDataPlane().export(con, data_dir, f=_ts(31))
    assert con.execute("SELECT count(*) FROM ev").fetchall() == [(6001,)]


def test_retention_trims_rows_moved_out_too(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "8192")
    con, data_dir = _con(), tmp_path / "data"
    for i in range(20):
        _write(con, data_dir, i)
    assert len(_local_runs(con)) < 20
    _write(con, data_dir, 20, retain_n=5)
    assert con.execute(f'SELECT min("{T.F_COL}"), count(*) FROM ev').fetchall() == [(_ts(16), 1000)]


def test_a_rebuilt_registry_holds_only_the_recent_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "8192")
    con, data_dir = _con(), tmp_path / "data"
    for i in range(25):
        _write(con, data_dir, i)
    fresh = _con()
    hydrate_registry(fresh, data_dir)
    assert len(_local_runs(fresh)) < 15
    assert fresh.execute("SELECT count(*), count(DISTINCT id) FROM ev").fetchall() == [(5000, 5000)]
    _write(fresh, data_dir, 25)  # and it goes on from there
    assert fresh.execute("SELECT count(*) FROM ev").fetchall() == [(5200,)]
