"""A merge Trickle's own name is a view over its current state, so plain SQL in a later Ripple reads what
``read_table`` returns. The cold base lives under ``{name}__base``. Covers the view through every storage
change (first write, warm fold, compaction, rebuild from the published layout), the migration of a legacy
registry that kept the base under the main's own name, the publish set, drops, and a Source view that
shares the name."""
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


def _current(con, name="dim"):
    return sorted(T.current_state(con, name).fetchall())


def _publish(con, data_dir, f):
    ParquetDataPlane().export(con, data_dir, f=f)


def test_plain_sql_reads_current_state_before_compaction(reg):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))
    assert _plain(reg) == [(1, "a"), (2, "b")]
    assert reg.sql("SELECT * FROM dim").columns == ["id", "v"]  # no system columns
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (3, "c")]), ts(2), ("id",))
    assert _plain(reg) == _current(reg) == [(1, "A"), (3, "c")]


def test_plain_sql_reads_current_state_after_compaction(reg):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))
    T.checkpoint(reg, "dim", ts(1))
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (2, "b")]), ts(2), ("id",))
    assert _plain(reg) == _current(reg) == [(1, "A"), (2, "b")]
    # The cold base is reachable by its own name, still at the compacted state with its freshness stamp.
    assert sorted(reg.sql("SELECT id, v FROM dim__base").fetchall()) == [(1, "a"), (2, "b")]
    assert "_duckstring_f" in reg.sql("SELECT * FROM dim__base").columns


def test_plain_sql_includes_the_warm_tier(reg):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(2), ("id",))
    T.fold_warm(reg, "dim", ts(1))  # moves the first run's changes into a warm band
    assert T._table_exists(reg, T.warm_name("dim"))
    assert _plain(reg) == _current(reg) == [(1, "a"), (2, "b")]


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
    assert set(T.load_sidecar(out)) - {"objects", "state"} == {"dim"}
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


@pytest.mark.parametrize("legacy_form", ["table", "view"])
def test_legacy_base_under_the_main_name_is_migrated(reg, legacy_form):
    """A registry from before this layout kept the cold base under the main's own name (a table, or a view
    over the published chunks after a rebuild). The first touch moves it to ``__base``."""
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))
    T.checkpoint(reg, "dim", ts(1))
    # Recreate the legacy layout: the base under "dim", no view.
    reg.execute("DROP VIEW dim")
    if legacy_form == "table":
        reg.execute("ALTER TABLE dim__base RENAME TO dim")
    else:
        reg.execute("CREATE TABLE legacy_store AS SELECT * FROM dim__base")
        reg.execute("DROP TABLE dim__base")
        reg.execute("CREATE VIEW dim AS SELECT * FROM legacy_store")
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "B")]), ts(2), ("id",))
    assert T._table_exists(reg, "dim__base")
    assert T._is_current_view(reg, "dim")
    assert _plain(reg) == [(1, "a"), (2, "B")]


def test_drop_table_removes_view_and_cold_base(reg):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    T.checkpoint(reg, "dim", ts(1))
    T.drop_table(reg, "dim")
    for name in ("dim", "dim__base", "dim__changelog"):
        assert not T._table_exists(reg, name)


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
