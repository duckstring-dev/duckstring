"""The data-plane seam (Phase 1 of plans/data-plane-iceberg.md): pluggable publish/consume of a
Pond's tables, write modes, and the reserved ``_duckstring_*`` system-column namespace."""

from __future__ import annotations

import duckdb
import pytest

from duckstring.dataplane import (
    ParquetDataPlane,
    ReservedColumnError,
    get_data_plane,
)


def test_parquet_roundtrip_write_then_read(tmp_path):
    dp = ParquetDataPlane()
    con = duckdb.connect()
    con.execute("CREATE TABLE event AS SELECT 1 AS id, 'a' AS val")
    dp.export(con, tmp_path)

    assert dp.list_tables(tmp_path) == ["event"]
    assert (tmp_path / "event.parquet").exists()
    assert dp.table_path(tmp_path, "event") == tmp_path / "event.parquet"

    rel = con.sql(dp.read_select(tmp_path, "event"))
    assert rel.fetchall() == [(1, "a")]


def test_overwrite_replaces_table(tmp_path):
    dp = ParquetDataPlane()
    con = duckdb.connect()
    con.execute("CREATE TABLE event AS SELECT 1 AS id")
    dp.export(con, tmp_path)
    con.execute("DROP TABLE event")
    con.execute("CREATE TABLE event AS SELECT 2 AS id")
    dp.export(con, tmp_path)
    assert con.sql(dp.read_select(tmp_path, "event")).fetchall() == [(2,)]


def test_reserved_namespace_rejected_at_write(tmp_path):
    dp = ParquetDataPlane()
    con = duckdb.connect()
    con.execute("CREATE TABLE event AS SELECT 1 AS id, 2 AS _duckstring_f")
    with pytest.raises(ReservedColumnError) as exc:
        dp.export(con, tmp_path)
    assert "_duckstring_f" in str(exc.value)
    # The whole prefix is reserved (not a single name): a sibling system column is rejected too.
    con.execute("DROP TABLE event")
    con.execute("CREATE TABLE event AS SELECT 1 AS id, 2 AS _duckstring_op")
    with pytest.raises(ReservedColumnError):
        dp.export(con, tmp_path)


def test_append_and_merge_modes_reserved(tmp_path):
    dp = ParquetDataPlane()
    con = duckdb.connect()
    con.execute("CREATE TABLE event AS SELECT 1 AS id")
    for mode in ("append", "merge"):
        with pytest.raises(NotImplementedError):
            dp.export(con, tmp_path, mode=mode)
    with pytest.raises(ValueError):
        dp.export(con, tmp_path, mode="bogus")


def test_read_missing_table_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ParquetDataPlane().read_select(tmp_path, "absent")


def test_get_data_plane_defaults_to_parquet(monkeypatch):
    monkeypatch.delenv("DUCKSTRING_DATA_PLANE", raising=False)
    assert isinstance(get_data_plane(), ParquetDataPlane)


def test_get_data_plane_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "iceberg")
    with pytest.raises(NotImplementedError):
        get_data_plane()
