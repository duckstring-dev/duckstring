"""An append Trickle's run that outgrows one file is published as size-bounded chunks, ``{part}__{i}.parquet``
(``dataplane._export_parts(chunk=True)``), which DuckDB writes in parallel: a 268M-row first load took 34.7 s
as one file and 6.1 s as chunks. Every reader treats a chunk as part of its run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb

from duckstring.dataplane import ParquetDataPlane
from duckstring.storage import LocalStorage
from duckstring.trickle import io as T

F1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
F2 = F1 + timedelta(hours=1)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # chunks of the 64 KiB floor
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    return con, LocalStorage(tmp_path / "data")


def _append(con, data, f, sql):
    T.append_table(con, "events", con.sql(sql), f, ("id",), fail_on_conflict=False)
    ParquetDataPlane().export(con, data, f=f)


def _rows(data, sql_from="events"):
    sql = ParquetDataPlane().read_select(data, sql_from)
    return duckdb.connect().sql(f"SELECT count(*), count(DISTINCT id) FROM ({sql})").fetchall()[0]


def test_a_large_run_is_published_as_chunks_and_read_whole(tmp_path, monkeypatch):
    con, data = _setup(tmp_path, monkeypatch)
    _append(con, data, F1, "SELECT range AS id, md5(range::VARCHAR) AS v FROM range(200000)")
    names = T.table_parts(data, "events")
    assert len(names) > 1 and all("__" in n for n in names)
    assert {T.part_f(n) for n in names} == {F1}
    assert _rows(data) == (200000, 200000)
    assert not (data.root / "events" / ".chunks").exists()

    _append(con, data, F2, "SELECT 200000 + range AS id, 'new' AS v FROM range(10)")  # a small run: one plain file
    assert T.part_name(F2) in T.table_parts(data, "events")
    assert _rows(data) == (200010, 200010)
    delta = T.read_delta(duckdb.connect(), data, "events", F1, F2, dp=ParquetDataPlane())
    assert not delta.is_full and delta.keys_count() == 10


def test_a_replayed_run_leaves_no_stale_chunks(tmp_path, monkeypatch):
    con, data = _setup(tmp_path, monkeypatch)
    _append(con, data, F1, "SELECT range AS id, md5(range::VARCHAR) AS v FROM range(200000)")
    _append(con, data, F1, "SELECT range AS id, 'x' AS v FROM range(5)")  # the same run again, much smaller
    assert T.table_parts(data, "events") == [T.part_name(F1)]
    assert _rows(data) == (5, 5)


def test_a_draw_ships_every_chunk_of_a_newer_run(tmp_path, monkeypatch):
    con, data = _setup(tmp_path, monkeypatch)
    _append(con, data, F1, "SELECT range AS id, 'a' AS v FROM range(10)")
    _append(con, data, F2, "SELECT 10 + range AS id, md5(range::VARCHAR) AS v FROM range(200000)")
    newer = [n for n in T.table_parts(data, "events") if T.part_f(n) > F1]
    assert len(newer) > 1  # what an incremental draw after F1 sends (routes/draw.py filters on part_f)
    assert T.part_f(T.part_name(F1)) == F1
