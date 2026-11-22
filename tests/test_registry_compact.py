"""A DuckDB file never shrinks: deleted rows leave free blocks it reuses but doesn't hand back. Once an append table's
older runs move out of the registry (`dataplane.fold_append`), `wa_views`' registry was 23 GiB with 98% of its blocks
free. A Duck opening a registry that is mostly free space copies the live data into a fresh file first."""

from __future__ import annotations

import duckdb
import pytest

from duckstring.duck import executor

pytestmark = pytest.mark.timeout(60)


def _registry(path, keep: int):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE _duckstring_hot_ev AS SELECT range AS id, repeat('x', 60) || range AS pad "
                "FROM range(3000000)")
    con.execute("CREATE VIEW ev AS SELECT * FROM _duckstring_hot_ev")
    con.execute("COMMENT ON VIEW ev IS 'duckstring:append-history'")
    con.execute("CREATE TABLE m (k INTEGER PRIMARY KEY, v VARCHAR)")
    con.execute("INSERT INTO m VALUES (1, 'x')")
    con.execute(f"DELETE FROM _duckstring_hot_ev WHERE id >= {keep}")
    con.execute("CHECKPOINT")
    con.close()


def test_a_mostly_free_registry_is_compacted_when_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "_COMPACT_MIN_BYTES", 1)
    path = tmp_path / "registry.duckdb"
    _registry(path, keep=100_000)
    before = path.stat().st_size
    con, recovery = executor.open_registry(path)
    assert recovery is None
    assert path.stat().st_size < before / 4
    assert con.execute("SELECT count(*), max(id) FROM ev").fetchall() == [(100_000, 99_999)]
    assert con.execute("SELECT comment FROM duckdb_views() WHERE view_name = 'ev'").fetchall() == [
        ("duckstring:append-history",)]
    with pytest.raises(duckdb.ConstraintException):  # the key survives too
        con.execute("INSERT INTO m VALUES (1, 'y')")
    con.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.duckdb"]  # nothing left beside it


def test_a_registry_mostly_in_use_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "_COMPACT_MIN_BYTES", 1)
    path = tmp_path / "registry.duckdb"
    _registry(path, keep=2_500_000)
    before = path.stat().st_mtime_ns, path.stat().st_size
    con, _ = executor.open_registry(path)
    con.close()
    assert path.stat().st_size == before[1]


def test_a_failed_compaction_keeps_the_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "_COMPACT_MIN_BYTES", 1)
    path = tmp_path / "registry.duckdb"
    _registry(path, keep=100_000)
    before = path.stat().st_size

    def boom(con, tmp):
        raise duckdb.IOException("disk full")

    monkeypatch.setattr(executor, "_copy_database", boom)
    con, recovery = executor.open_registry(path)
    assert recovery is None and path.stat().st_size == before
    assert con.execute("SELECT count(*) FROM ev").fetchall() == [(100_000,)]
    con.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.duckdb"]


def test_the_spill_directory_is_set_before_anything_runs(tmp_path, monkeypatch):
    """DuckDB refuses to switch its temporary directory once a query has spilled: a Duck that ran (or compacted its
    registry) before being told its memory limit failed its next Run setting it (the attention showcase)."""
    monkeypatch.setattr(executor, "_COMPACT_MIN_BYTES", 1)
    spill = tmp_path / "tmp" / "p_m1"
    for keep in (100_000, 2_500_000):  # compacted on open, and not
        path = tmp_path / f"r{keep}.duckdb"
        _registry(path, keep=keep)
        con, _ = executor.open_registry(path, spill)
        assert con.execute("SELECT current_setting('temp_directory')").fetchall() == [(str(spill),)]
        con.close()
    con, recovery = executor.open_registry(tmp_path / "new.duckdb", spill)
    assert recovery == "missing"
    assert con.execute("SELECT current_setting('temp_directory')").fetchall() == [(str(spill),)]
