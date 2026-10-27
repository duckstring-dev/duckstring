"""A merge table's first load in a Pond: written once, as Parquet staged beside the published data
(``dataplane.stage_boot_base``), with the registry's base a view over it until the publish moves it into
``{table}__base/`` (``dataplane._publish_local_base``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb

from duckstring.core import Pond
from duckstring.dataplane import BOOT_STAGING, ParquetDataPlane
from duckstring.storage import LocalStorage
from duckstring.trickle.io import base_dir_name, cold_base_name, first_row, part_name

F1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
F2 = F1 + timedelta(hours=1)
F3 = F1 + timedelta(hours=2)


class Producer:
    def __init__(self, root):
        self.root = root
        self.data_dir = LocalStorage(root / "ponds" / "src" / "m1" / "data")
        self.con = duckdb.connect(str(root / "src.duckdb"))

    def merge(self, f, sql, **kw):
        pond = Pond("src", "1.0.0", self.con, self.root, f=f, own_data_dir=self.data_dir)
        pond.merge_table("m", self.con.sql(sql), pk="id", **kw)

    def publish(self, f):
        ParquetDataPlane().export(self.con, self.data_dir, f=f)

    def staged(self):
        d = self.data_dir.root / BOOT_STAGING / "m"
        return sorted(p.name for p in d.glob("*.parquet")) if d.is_dir() else []

    def chunks(self):
        return sorted(p.name for p in (self.data_dir.root / base_dir_name("m")).glob("*.parquet"))

    def base_sql(self):
        return first_row(self.con.execute("SELECT sql FROM duckdb_views() WHERE view_name = ?",
                                          [cold_base_name("m")]))[0]

    def published(self, proj="id, v"):
        sql = ParquetDataPlane().read_select(self.data_dir, "m")
        return sorted(duckdb.connect().sql(f"SELECT {proj} FROM ({sql})").fetchall())


def _rows(n, label="a"):
    return f"SELECT range AS id, '{label}' || range AS v FROM range({n})"


def test_a_first_load_is_staged_once_and_moved_into_place(tmp_path):
    p = Producer(tmp_path)
    p.merge(F1, _rows(1000))
    assert p.staged() and BOOT_STAGING in p.base_sql()  # the base is a view over the staged files
    assert first_row(p.con.execute("SELECT count(*) FROM m"))[0] == 1000  # and reads as the table

    p.publish(F1)
    token = part_name(F1)[: -len(".parquet")]
    assert p.chunks() and all(c.startswith(token) for c in p.chunks())
    assert not p.staged() and BOOT_STAGING not in p.base_sql()  # now a view over the published chunks
    assert len(p.published()) == 1000

    p.merge(F2, f"{_rows(1000)} WHERE id <> 7 UNION ALL SELECT 1000, 'new'")  # the next run is a change
    p.publish(F2)
    assert len(p.published()) == 1000 and (1000, "new") in p.published() and (7, "a7") not in p.published()


def test_a_replay_before_the_publish_replaces_what_was_staged(tmp_path):
    p = Producer(tmp_path)
    p.merge(F1, _rows(10))
    p.merge(F1, _rows(5, "b"))  # the bootstrap replayed with different data, before its publish
    p.publish(F1)
    assert p.published() == [(i, f"b{i}") for i in range(5)]


def test_a_rebuilds_first_load_replaces_the_published_base(tmp_path):
    p = Producer(tmp_path)
    p.merge(F1, _rows(10))
    p.publish(F1)
    for (name,) in p.con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall():
        p.con.execute(f'DROP VIEW "{name}"')  # a Rebuild's wipe
    for (name,) in p.con.execute("SELECT table_name FROM duckdb_tables()").fetchall():
        p.con.execute(f'DROP TABLE "{name}"')
    p.merge(F3, _rows(3, "c"))
    p.publish(F3)
    token = part_name(F3)[: -len(".parquet")]
    assert all(c.startswith(token) for c in p.chunks())
    assert p.published() == [(i, f"c{i}") for i in range(3)]


def test_a_declared_cluster_by_is_applied_at_the_first_publish(tmp_path):
    p = Producer(tmp_path)
    p.merge(F1, "SELECT range AS id, 999 - range AS v FROM range(1000)", cluster_by="v")
    p.publish(F1)
    chunk = p.data_dir.root / base_dir_name("m") / p.chunks()[0]
    vs = [r[0] for r in duckdb.connect().sql(f"SELECT v FROM read_parquet('{chunk}')").fetchall()]
    assert vs == sorted(vs)  # rewritten in v order, not the order the rows arrived in
    assert not p.staged()


def test_a_join_on_changed_keys_reaches_a_sources_base_scan(tmp_path):
    """A Sink's join of a Source merge table with a few keys reads only the rows of those keys, not the whole
    base: the Source's changelog consolidation is a temporary table, so nothing stops the join's key filter
    (a CTE DuckDB materialises did, and every incremental join term scanned the whole fact)."""
    import json

    p = Producer(tmp_path)
    p.merge(F1, "SELECT range AS id, range % 1000 AS grp, 'v' || range AS v FROM range(500000)")
    p.publish(F1)
    p.merge(F2, "SELECT range AS id, range % 1000 AS grp, CASE WHEN range = 7 THEN 'x' ELSE 'v' || range END AS v "
                "FROM range(500000)")
    p.publish(F2)
    con = duckdb.connect()
    sink = Pond("snk", "1.0.0", con, tmp_path, source_majors={"src": 1}, f=F2, previous_f=F1,
                source_f={"src": F2.isoformat()})
    sink.read_table("src.m").create_view("src_m")
    con.execute("CREATE TABLE keys AS SELECT * FROM (VALUES (3), (500)) t(g)")
    out = tmp_path / "profile.json"
    con.execute("SET enable_profiling = 'json'")
    con.execute(f"SET profiling_output = '{out}'")
    rows = con.execute("SELECT count(*) FROM src_m SEMI JOIN keys ON src_m.grp = keys.g").fetchall()[0][0]
    con.execute("SET enable_profiling = 'no_output'")
    assert rows == 1000
    profile = json.loads(out.read_text())
    if not isinstance(profile.get("query"), dict):  # DuckDB 1.5: a different profile, and it prunes this scan only
        return                                       # by row group, so the guard below is 2.0's behaviour
    assert profile["query"]["total_rows_scanned"] < 100_000  # of 500,000
