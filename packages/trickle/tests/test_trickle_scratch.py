"""What writes leave in the database, and writers on several threads with a cursor each over one file database
(how a host like Duckstring runs a Pond's Ripples). Every first write creates the shared meta table, and two
threads creating it at once failed with "Catalog write-write conflict on create" (the transit showcase, 3 in 6
first Runs); so did two creating the append's fixed-name scratch view."""

from __future__ import annotations

import threading

import duckdb
import pytest
import trickle
from trickle import agg
from trickle_support import rows, ts

pytestmark = pytest.mark.timeout(120)


def _writers(con, f):
    """Eight writes of every kind a Ripple makes: merge, append (with and without a key), an aggregate and a
    builder join, each on its own cursor and table."""

    def merge(i):
        c = con.cursor()
        trickle.at(c, f).merge_table(f"m{i}", c.sql(f"SELECT range AS k, {i} AS v FROM range(20)"), pk="k")

    def append(i):
        c = con.cursor()
        trickle.at(c, f).append_table(f"a{i}", c.sql(f"SELECT range AS k, {i} AS v FROM range(20)"),
                                      pk=("k" if i % 2 else None))

    def aggregate(i):
        c = con.cursor()
        (trickle.at(c, f).source("src").aggregate(by="g", n=agg.count(), s=agg.sum("v"))
         .merge(f"g{i}"))

    def join(i):
        c = con.cursor()
        (trickle.at(c, f).source("src").join(trickle.at(c, f).source("dim"), on="g")
         .select("s0.k, s0.v, s1.label").merge(f"j{i}", pk="k"))

    return [lambda i=i: merge(i) for i in range(2)] + [lambda i=i: append(i) for i in range(2)] \
        + [lambda i=i: aggregate(i) for i in range(2)] + [lambda i=i: join(i) for i in range(2)]


@pytest.mark.parametrize("trial", range(25))
def test_writers_on_threads_share_a_new_database(tmp_path, trial):
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    con.execute("CREATE TABLE src AS SELECT range AS k, range % 3 AS g, range AS v FROM range(30)")
    con.execute("CREATE TABLE dim AS SELECT range AS g, 'g' || range AS label FROM range(3)")
    f = ts(1)
    errors: list[BaseException] = []

    def guard(work):
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=guard, args=(w,)) for w in _writers(con, f)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[0]
    assert len(rows(con, "m1")) == 20 and len(rows(con, "j0")) == 30
    assert sorted(rows(con, "g0")) == [(0, 10, 135), (1, 10, 145), (2, 10, 155)]


def _stored_views(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT view_name FROM duckdb_views() WHERE NOT temporary AND NOT internal ORDER BY 1").fetchall()]


def test_writes_leave_no_views_in_the_database(tmp_path):
    """Every scratch view is temporary: a database kept eight per incremental write, and the catalog lookups each
    write makes slowed with them (the transit showcase's Run times grew with their count)."""
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    con.execute("CREATE TABLE dim AS SELECT range AS g, 'g' || range AS label FROM range(3)")
    prev = trickle.NEVER
    for i in range(1, 6):
        c = con.cursor()
        f = ts(i)
        trickle.at(c, f, prev).append_table("obs", c.sql(f"SELECT {i} * 10 + range AS k, range % 3 AS g, range AS v "
                                                         f"FROM range(10)"), pk="k")
        ep = trickle.at(c, f, prev)
        ep.source("obs").join(ep.source("dim"), on="g").select("s0.k, s0.v, s1.label").append("wide", pk="k")
        trickle.at(c, f, prev).source("obs").aggregate(by="g", n=agg.count()).merge("per_g")
        trickle.at(c, f, prev).source("obs").alias("o").sql("SELECT g, max(v) AS top FROM o GROUP BY g") \
            .merge("top", pk="g")
        c.close()
        prev = f
    assert _stored_views(con) == ["per_g", "top"]  # the merge tables' current-state views, nothing else
    assert len(rows(con, "wide")) == 50


def test_leftover_scratch_views_are_dropped(tmp_path):
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    trickle.at(con, ts(1)).merge_table("m", con.sql("SELECT 1 AS k"), pk="k")
    con.sql("SELECT 1 AS x").create_view(trickle.io.unique_name("old"))  # as 0.3.1 stored them
    con.execute("CREATE VIEW mine AS SELECT 2 AS y")
    assert trickle.io.drop_scratch_views(con) == 1
    assert _stored_views(con) == ["m", "mine"]
    assert rows(con, "m") == [(1,)]


def test_append_from_arrow_in_a_file_database(tmp_path):
    """A relation over Python data can only be named by a temporary view; the append's scratch view was stored,
    which DuckDB 2.0 refuses for a file database ("it reads a Python object")."""
    pa = pytest.importorskip("pyarrow")
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    batch = pa.table({"k": [1, 2], "v": ["a", "b"]})
    trickle.at(con, ts(1)).append_table("t", con.from_arrow(batch), pk="k")
    assert rows(con, "t") == [(1, "a"), (2, "b")]
