"""The Iceberg data-plane backend (Phase 1 of plans/data-plane-iceberg.md): pyiceberg overwrite
commits with an ``f``-stamped snapshot per run, DuckDB ``iceberg_scan`` reads, the as-of resolver, the
reserved-namespace guard, and the flat-Parquet sidecar + legacy fallback. Skipped without the extra."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

pytest.importorskip("pyiceberg")  # the only data-plane dep; SQLAlchemy is deliberately NOT required

from duckstring.dataplane import ReservedColumnError, get_data_plane  # noqa: E402
from duckstring.iceberg_plane import F_PROP, IcebergDataPlane  # noqa: E402

UTC = timezone.utc


def _con(sql: str):
    con = duckdb.connect()
    con.execute(sql)
    return con


def test_roundtrip_write_then_read(tmp_path):
    dp = IcebergDataPlane()
    con = _con("CREATE TABLE event AS SELECT * FROM (VALUES (1,'a'),(2,'b')) t(id,val)")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, tzinfo=UTC))

    assert dp.list_tables(tmp_path) == ["event"]
    rcon = duckdb.connect()
    dp.prepare(rcon)
    assert sorted(rcon.sql(dp.read_select(tmp_path, "event")).fetchall()) == [(1, "a"), (2, "b")]


def test_flat_parquet_sidecar_and_catalog_written(tmp_path):
    dp = IcebergDataPlane()
    dp.export(_con("CREATE TABLE event AS SELECT 1 AS id"), tmp_path, f=datetime(2026, 6, 16, tzinfo=UTC))
    # The compat sidecar (for draws / direct-serve / fallback) and the per-line catalog both exist.
    assert (tmp_path / "event.parquet").exists()
    assert (tmp_path / "catalog.json").exists()
    assert dp.table_path(tmp_path, "event") == tmp_path / "event.parquet"


def test_one_snapshot_per_run_stamped_with_f(tmp_path):
    dp = IcebergDataPlane()
    con = _con("CREATE TABLE event AS SELECT 1 AS id")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 0, tzinfo=UTC))
    con.execute("DROP TABLE event; CREATE TABLE event AS SELECT 2 AS id")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 1, tzinfo=UTC))

    tbl = dp._load(tmp_path, "event")
    stamps = {
        s.summary.additional_properties.get(F_PROP)
        for s in tbl.snapshots() if s.summary
    }
    assert "2026-06-16T00:00:00+00:00" in stamps
    assert "2026-06-16T01:00:00+00:00" in stamps


def test_as_of_reads_the_snapshot_for_that_freshness(tmp_path):
    dp = IcebergDataPlane()
    con = _con("CREATE TABLE event AS SELECT * FROM (VALUES (1,'a'),(2,'b')) t(id,val)")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 0, tzinfo=UTC))
    con.execute("DROP TABLE event; CREATE TABLE event AS SELECT * FROM (VALUES (9,'z')) t(id,val)")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 1, tzinfo=UTC))

    rcon = duckdb.connect()
    dp.prepare(rcon)
    latest = dp.read_select(tmp_path, "event")
    as_of = dp.read_select(tmp_path, "event", as_of=datetime(2026, 6, 16, 0, 30, tzinfo=UTC))
    assert sorted(rcon.sql(latest).fetchall()) == [(9, "z")]
    assert sorted(rcon.sql(as_of).fetchall()) == [(1, "a"), (2, "b")]


def test_reserved_namespace_rejected_before_any_commit(tmp_path):
    dp = IcebergDataPlane()
    con = _con("CREATE TABLE event AS SELECT 1 AS id, 2 AS _duckstring_f")
    with pytest.raises(ReservedColumnError):
        dp.export(con, tmp_path, f=datetime(2026, 6, 16, tzinfo=UTC))
    assert dp._load(tmp_path, "event") is None  # nothing committed


def test_fallback_to_flat_parquet_when_no_catalog(tmp_path):
    # A Source published by the legacy Parquet plane (no Iceberg catalog) is still readable.
    duckdb.sql("SELECT 7 AS id").write_parquet(str(tmp_path / "event.parquet"))
    dp = IcebergDataPlane()
    rcon = duckdb.connect()
    dp.prepare(rcon)
    assert rcon.sql(dp.read_select(tmp_path, "event")).fetchall() == [(7,)]


def test_schema_change_recreates_and_keeps_exporting(tmp_path):
    dp = IcebergDataPlane()
    con = _con("CREATE TABLE event AS SELECT 1 AS id")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 0, tzinfo=UTC))
    con.execute("DROP TABLE event; CREATE TABLE event AS SELECT 1 AS id, 'x' AS extra")
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 1, tzinfo=UTC))  # additive change must not break export
    rcon = duckdb.connect()
    dp.prepare(rcon)
    assert rcon.sql(dp.read_select(tmp_path, "event")).fetchall() == [(1, "x")]


def test_get_data_plane_iceberg_via_env(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "iceberg")
    assert isinstance(get_data_plane(), IcebergDataPlane)


def test_pond_read_table_foreign_under_iceberg(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "iceberg")
    from duckstring.core import Pond

    src_data = tmp_path / "ponds" / "src" / "m1" / "data"
    src_data.mkdir(parents=True)
    get_data_plane().export(
        _con("CREATE TABLE event AS SELECT * FROM (VALUES (1,'a')) t(id,val)"),
        src_data, f=datetime(2026, 6, 16, tzinfo=UTC),
    )

    rcon = duckdb.connect()
    pond = Pond("snk", "1.0.0", rcon, root=tmp_path, source_majors={"src": 1})
    rel = pond.read_table("src.event")
    assert rel.fetchall() == [(1, "a")]
    # The registered view (referenced by table name) reads the Iceberg snapshot.
    assert rcon.sql("SELECT val FROM event WHERE id = 1").fetchall() == [("a",)]


def test_trickle_append_commits_are_incremental_and_window_reads(tmp_path):
    # An append Trickle published over Iceberg commits one _duckstring_f-homogeneous data file per run
    # (small writes), and the window read returns only the rows in (previous_f, f].
    from duckstring import trickle_io as T

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    dp = IcebergDataPlane()

    def run(idval, hour):
        T.append_table(con, "event", con.sql(f"SELECT {idval} AS id"),
                       datetime(2026, 6, 16, hour, tzinfo=UTC), ("id",))
        dp.export(con, tmp_path, f=datetime(2026, 6, 16, hour, tzinfo=UTC))

    run(1, 1)
    run(2, 2)
    run(3, 3)

    # Three append commits → three snapshots (one per run), not a wholesale rewrite each time.
    tbl = dp._load(tmp_path, "event")
    assert len([s for s in tbl.snapshots() if s.summary and s.summary.additional_properties.get(F_PROP)]) == 3
    # The sidecar travelled, so a fresh reader resolves the append mode and windows correctly.
    assert T.load_sidecar(tmp_path)["event"]["mode"] == "append"
    rcon = duckdb.connect()
    dp.prepare(rcon)
    d = T.read_delta(rcon, tmp_path, "event",
                     previous_f=datetime(2026, 6, 16, 1, tzinfo=UTC),
                     f=datetime(2026, 6, 16, 3, tzinfo=UTC), dp=dp)
    assert sorted(d.upserts.fetchall()) == [(2,), (3,)]  # excludes run 1, includes run 3


def test_trickle_merge_main_overwrite_changelog_append_over_iceberg(tmp_path):
    # A merge Trickle: the clean main is overwritten (current state, no tombstones) while the changelog
    # grows by append — and a delta read collapses the changelog window per PK to the latest op.
    from duckstring import trickle_io as T

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    dp = IcebergDataPlane()

    def state(rows, hour):
        vals = ", ".join(f"({i}, '{v}')" for i, v in rows)
        T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {vals}) t(id, v)"),
                      datetime(2026, 6, 16, hour, tzinfo=UTC), ("id",), comprehensive=True)
        dp.export(con, tmp_path, f=datetime(2026, 6, 16, hour, tzinfo=UTC))

    state([(1, "a"), (2, "b")], 1)
    state([(1, "A")], 2)  # 1 updated, 2 deleted

    rcon = duckdb.connect()
    dp.prepare(rcon)
    # The main is the clean current state, system columns stripped.
    assert sorted(T._strip_system(rcon.sql(dp.read_select(tmp_path, "dim"))).fetchall()) == [(1, "A")]
    d = T.read_delta(rcon, tmp_path, "dim",
                     previous_f=datetime(2026, 6, 16, 1, tzinfo=UTC),
                     f=datetime(2026, 6, 16, 2, tzinfo=UTC), dp=dp)
    assert sorted(d.upserts.fetchall()) == [(1, "A")]
    assert d.deletes.fetchall() == [(2,)]


def test_trickle_retention_drops_expired_iceberg_files(tmp_path):
    # Registry retention (retain_n) trims old changelog/history rows; the Iceberg history mirrors it by
    # dropping whole expired files (floor advances), so it doesn't grow unbounded. Space-only — a reader
    # behind the floor still reads correctly via the full-read fallback.
    from duckstring import trickle_io as T
    from duckstring.iceberg_plane import FLOOR_PROP

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    dp = IcebergDataPlane()
    for h in (1, 2, 3, 4):
        T.append_table(con, "event", con.sql(f"SELECT {h} AS id"),
                       datetime(2026, 6, 16, h, tzinfo=UTC), ("id",), retain_n=2)
        dp.export(con, tmp_path, f=datetime(2026, 6, 16, h, tzinfo=UTC))

    tbl = dp._load(tmp_path, "event")
    assert tbl.properties[FLOOR_PROP] == "2026-06-16T03:00:00+00:00"  # newest 2 runs retained
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    dp.prepare(rcon)
    assert sorted(r[0] for r in rcon.sql(dp.read_select(tmp_path, "event")).fetchall()) == [3, 4]


def test_trickle_iceberg_replay_appends_no_duplicates(tmp_path):
    # The append cursor is a table property, so a replay at the same f (or a re-export) adds nothing —
    # even after a retention delete-snapshot (which must not clobber the cursor).
    from duckstring import trickle_io as T

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    dp = IcebergDataPlane()
    T.append_table(con, "event", con.sql("SELECT 1 AS id"), datetime(2026, 6, 16, 1, tzinfo=UTC), ("id",))
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 1, tzinfo=UTC))
    dp.export(con, tmp_path, f=datetime(2026, 6, 16, 1, tzinfo=UTC))  # replay / re-export at the same f

    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    dp.prepare(rcon)
    assert sorted(r[0] for r in rcon.sql(dp.read_select(tmp_path, "event")).fetchall()) == [1]  # no dup
