"""A merge Trickle's own name is a view over its current state, so plain SQL in a later Ripple reads what
``read_table`` returns. The engine tests cover the view itself (``tests/trickle/test_trickle_io.py``); these
cover what Duckstring adds: publishing through compaction, rebuilding the registry from the published
layout, and a Source view that shares the name."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane, hydrate_registry

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


@pytest.fixture
def reg(tmp_path):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    yield con
    con.close()


def _state(con, pairs):
    values = ", ".join(f"({i}, '{v}')" for i, v in pairs)
    return con.sql(f"SELECT * FROM (VALUES {values}) AS s(id, v)")


def _plain(con, name="dim"):
    return sorted(con.sql(f"SELECT * FROM {name}").fetchall())


def _publish(con, data_dir, f):
    ParquetDataPlane().export(con, data_dir, f=f)


def test_publish_through_compaction_keeps_the_view_and_the_published_set(reg, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # compact on every publish
    out = tmp_path / "data"
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))
    _publish(reg, out, ts(1))
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (3, "c")]), ts(2), ("id",))
    _publish(reg, out, ts(2))
    assert _plain(reg) == [(1, "A"), (3, "c")]
    # Nothing new is published: no file or sidecar entry for the registry's __base relation or the view.
    assert not (out / "dim__base.parquet").exists()
    assert not (out / "dim.parquet").exists()
    assert set(T.sidecar_tables(T.load_sidecar(out))) == {"dim"}
    assert T.base_chunks(out, "dim")  # the published base is still the chunk directory
    read = ParquetDataPlane().read_select(out, "dim")
    assert sorted(reg.sql(f"SELECT id, v FROM ({read})").fetchall()) == [(1, "A"), (3, "c")]


def test_rebuild_from_published_layout_restores_the_view(reg, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")
    out = tmp_path / "data"
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))
    _publish(reg, out, ts(1))
    T.merge_table(reg, "dim", _state(reg, [(1, "A")]), ts(2), ("id",))
    _publish(reg, out, ts(2))
    fresh = duckdb.connect(str(tmp_path / "fresh.duckdb"))
    try:
        hydrate_registry(fresh, out)
        assert T._is_current_view(fresh, "dim")
        assert _plain(fresh) == [(1, "A")]
    finally:
        fresh.close()


def test_source_view_does_not_replace_the_merge_view(reg, tmp_path):
    """``read_table("src.dim")`` registers the Source table as a view named ``dim``. When the Pond has its own
    merge Trickle ``dim``, that view is kept, and the Source is still returned as a relation."""
    src_dir = tmp_path / "ponds" / "src" / "data"
    src = duckdb.connect()
    src.execute("CREATE TABLE dim AS SELECT 99 AS id, 'source' AS v")
    ParquetDataPlane().export(src, src_dir)
    src.close()

    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    pond = Pond("me", "1.0.0", reg, root=tmp_path, f=ts(2))
    assert pond.read_table("src.dim").fetchall() == [(99, "source")]
    assert _plain(reg) == [(1, "a")]
