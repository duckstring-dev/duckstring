"""The warm tier read where it's published (plans/s3-resident-state.md): a fold stages its band, the publish
writes it as a band file and swaps it into a view over the band files, and a fresh registry registers that
view instead of copying the bands in. Persist prunes bands only once a checkpoint has folded them."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.dataplane import ParquetDataPlane, hydrate_registry, persist_tree, restore_tree
from duckstring.storage import LocalStorage

F0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def ts(h: int) -> datetime:
    return F0 + timedelta(hours=h)


@pytest.fixture(autouse=True)
def _no_auto_compaction(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", str(1 << 40))  # folds and checkpoints only by hand


def _con():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def _merge(con, h: int, rows):
    values = ", ".join(f"({i}, '{v}')" for i, v in rows)
    T.merge_table(con, "m", con.sql(f"SELECT * FROM (VALUES {values}) t(id, v)"), ts(h), ("id",))


def _current(con):
    return sorted(con.sql('SELECT id, v FROM "m"').fetchall())


def _is_view(con, name):
    return con.execute("SELECT 1 FROM duckdb_views() WHERE view_name = ?", [name]).fetchone() is not None


def _publish(con, out, h):
    ParquetDataPlane().export(con, out, f=ts(h))


def _bands(out):
    return sorted(p.name for p in (out / "m__band").glob("*.parquet"))


def _three_runs_one_fold(con, out):
    """Three merge runs; the first two folded into one published band."""
    _merge(con, 1, [(1, "a"), (2, "b")])
    _publish(con, out, 1)
    _merge(con, 2, [(1, "A"), (2, "b"), (3, "c")])
    _publish(con, out, 2)
    T.fold_warm(con, "m", ts(2))
    _merge(con, 3, [(1, "A"), (3, "c"), (4, "d")])
    _publish(con, out, 3)


def test_a_published_fold_becomes_a_view_over_band_files(tmp_path):
    con, out = _con(), tmp_path / "data"
    _three_runs_one_fold(con, out)
    assert _bands(out) == [T.part_name(ts(2))]
    assert _is_view(con, "m__band")
    assert not T._table_exists(con, T.warm_pending_name("m"))
    assert _current(con) == [(1, "A"), (3, "c"), (4, "d")]
    # The warm tier is no longer also published as a stray whole-table file on every export.
    assert not (out / "m__band.parquet").exists()
    read = ParquetDataPlane().read_select(out, "m")
    assert sorted(con.sql(f"SELECT id, v FROM ({read})").fetchall()) == [(1, "A"), (3, "c"), (4, "d")]


def test_a_fresh_registry_reads_bands_where_they_are_published(tmp_path):
    con, out = _con(), tmp_path / "data"
    _three_runs_one_fold(con, out)
    fresh = _con()
    hydrate_registry(fresh, out)
    assert _is_view(fresh, "m__band")  # registered, not copied
    assert T._f_warm(fresh, "m") == ts(2)
    assert _current(fresh) == [(1, "A"), (3, "c"), (4, "d")]
    # It carries on: a further run and fold publish a second band beside the first.
    _merge(fresh, 4, [(1, "A"), (4, "d")])
    _publish(fresh, out, 4)
    T.fold_warm(fresh, "m", ts(3))
    _publish(fresh, out, 4)
    assert _bands(out) == [T.part_name(ts(2)), T.part_name(ts(3))]
    assert _current(fresh) == [(1, "A"), (4, "d")]


def test_bands_at_or_below_the_base_are_ignored(tmp_path):
    con, out = _con(), tmp_path / "data"
    _three_runs_one_fold(con, out)
    shutil.copy(out / "m__band" / T.part_name(ts(2)), out / "m__band" / T.part_name(ts(0)))  # a stale band
    sidecar = T.load_sidecar(out)
    sidecar["m"]["f_base"] = ts(0).isoformat()
    T.write_sidecar(out, sidecar)
    fresh = _con()
    hydrate_registry(fresh, out)
    sql = fresh.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'm__band'").fetchone()[0]
    assert T.part_name(ts(0)) not in sql and T.part_name(ts(2)) in sql


def test_a_lost_local_publish_is_restored_before_hydrating(tmp_path):
    """A Duck whose machine lost its local publish restores it from the durable layer, sidecar last, so the
    registry's views and the Ponds reading the local publish see a complete layout (base chunks too)."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # checkpoint the first run, so a base exists
    con, local, durable = _con(), tmp_path / "local", tmp_path / "durable"
    _merge(con, 1, [(1, "a"), (2, "b")])
    _publish(con, local, 1)
    monkeypatch.undo()
    _merge(con, 2, [(1, "A"), (2, "b"), (3, "c")])
    _publish(con, local, 2)
    T.fold_warm(con, "m", ts(2))
    _publish(con, local, 2)
    persist_tree(LocalStorage(local), LocalStorage(durable))
    assert T.base_chunks(durable, "m") and _bands(durable)

    shutil.rmtree(local)
    restore_tree(LocalStorage(durable), LocalStorage(local))
    assert T.base_chunks(local, "m") == T.base_chunks(durable, "m") and _bands(local) == _bands(durable)
    fresh = _con()
    hydrate_registry(fresh, local)
    sql = fresh.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'm__band'").fetchone()[0]
    assert str(local) in sql and str(durable) not in sql
    assert _current(fresh) == [(1, "A"), (2, "b"), (3, "c")]


def test_a_legacy_warm_table_is_converted_without_double_counting(tmp_path):
    con, out = _con(), tmp_path / "data"
    _three_runs_one_fold(con, out)
    # The old layout: the warm tier as a table holding the published band's rows, plus one never published.
    con.execute("DROP VIEW m__band")
    band = out / "m__band" / T.part_name(ts(2))
    con.execute(f"CREATE TABLE m__band AS SELECT * FROM read_parquet('{band}')")
    con.execute("INSERT INTO m__band VALUES (9, 'z', 1, TIMESTAMPTZ '2026-06-01 02:30:00+00')")
    T._set_f_warm(con, "m", ts(2) + timedelta(minutes=30))
    _publish(con, out, 3)
    assert _is_view(con, "m__band")
    assert len(_bands(out)) == 2
    assert _current(con) == [(1, "A"), (3, "c"), (4, "d"), (9, "z")]


def test_a_band_staged_before_a_crash_is_published_by_the_next_export(tmp_path):
    con, out = _con(), tmp_path / "data"
    _merge(con, 1, [(1, "a")])
    _publish(con, out, 1)
    _merge(con, 2, [(1, "b")])
    T.fold_warm(con, "m", ts(1))  # staged, then the Duck dies before publishing
    assert T._table_exists(con, T.warm_pending_name("m"))
    assert _current(con) == [(1, "b")]
    _publish(con, out, 2)
    assert _bands(out) == [T.part_name(ts(1))] and _is_view(con, "m__band")
    assert _current(con) == [(1, "b")]


def test_persist_keeps_live_bands_as_the_floor_rises_and_prunes_folded_ones(tmp_path, monkeypatch):
    con, local, durable = _con(), tmp_path / "local", tmp_path / "durable"
    _three_runs_one_fold(con, local)
    T.fold_warm(con, "m", ts(3))
    _publish(con, local, 3)
    persist_tree(LocalStorage(local), LocalStorage(durable))
    assert _bands(durable) == [T.part_name(ts(2)), T.part_name(ts(3))]

    # A Duck that lost its local publish rebuilds from the durable layer; publishing and persisting from it
    # must not cost the durable layer the older band (the floor is above it, but it's still live state).
    shutil.rmtree(local)
    restore_tree(LocalStorage(durable), LocalStorage(local))
    fresh = _con()
    hydrate_registry(fresh, local)
    _merge(fresh, 4, [(1, "A"), (4, "d")])
    T.fold_warm(fresh, "m", ts(4))  # a fold after the rebuild: the case that used to lose older bands
    _publish(fresh, local, 4)
    persist_tree(LocalStorage(local), LocalStorage(durable))
    assert _bands(durable) == [T.part_name(ts(2)), T.part_name(ts(3)), T.part_name(ts(4))]
    again = _con()
    hydrate_registry(again, durable)
    assert _current(again) == [(1, "A"), (4, "d")]

    # A checkpoint folds the bands into the base and removes the local band directory; persist follows.
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")
    _merge(fresh, 5, [(1, "A"), (5, "e")])
    _publish(fresh, local, 5)
    assert not (local / "m__band").exists() and not _is_view(fresh, "m__band")
    persist_tree(LocalStorage(local), LocalStorage(durable))
    assert _bands(durable) == []
    last = _con()
    hydrate_registry(last, durable)
    assert _current(last) == [(1, "A"), (5, "e")]
