"""The standalone Trickle host (plans/trickle-extraction.md, phase 2): ``trickle.run(con, pipeline=...)`` keeps
tables in one DuckDB database up to date incrementally, with its run state in the database. These tests use
only the engine and that host, so they move with the package when it's extracted."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
import trickle
from trickle import NEVER, agg
from trickle.session import RUN_TABLE, Run

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path):
    c = duckdb.connect(str(tmp_path / "shop.duckdb"))
    c.execute("CREATE TABLE raw_orders (order_id INTEGER, product_id INTEGER, quantity INTEGER)")
    c.execute("INSERT INTO raw_orders VALUES (1, 10, 2), (2, 11, 1), (3, 10, 5)")
    c.execute("CREATE TABLE raw_products (product_id INTEGER, category VARCHAR, unit_price DECIMAL(10, 2))")
    c.execute("INSERT INTO raw_products VALUES (10, 'Books', 12.50), (11, 'Home', 30.00)")
    yield c
    c.close()


def _paths(monkeypatch):
    """Which write each builder terminal used: 'comprehensive' (merge_table) or 'incremental' (apply_zset)."""
    paths: list[str] = []
    orig_m, orig_z = Run.merge_table, Run.apply_zset

    def spy_m(self, name, *a, **k):
        if name not in ("orders", "products"):
            paths.append("comprehensive")
        return orig_m(self, name, *a, **k)

    def spy_z(self, *a, **k):
        paths.append("incremental")
        return orig_z(self, *a, **k)

    monkeypatch.setattr(Run, "merge_table", spy_m)
    monkeypatch.setattr(Run, "apply_zset", spy_z)
    return paths


def _pipeline(t, con):
    """The README pipeline: snapshots in as merges, a join and an aggregate out."""
    t.merge_table("orders", con.sql("FROM raw_orders"), pk="order_id")
    t.merge_table("products", con.sql("FROM raw_products"), pk="product_id")
    (t.source("orders")
       .join(t.source("products"), on="product_id")
       .select("s1.category, s0.quantity * s1.unit_price AS revenue")
       .aggregate(by="category", revenue=agg.sum("revenue"), orders=agg.count())
       .merge("revenue_by_category"))


def _scratch(con):
    return sorted(con.sql("""
        SELECT p.category, sum(o.quantity * p.unit_price), count(*)
        FROM raw_orders o JOIN raw_products p USING (product_id) GROUP BY p.category
    """).fetchall())


def _out(con):
    return sorted(con.sql("SELECT category, revenue, orders FROM revenue_by_category").fetchall())


def _join(t, con):
    t.merge_table("orders", con.sql("FROM raw_orders"), pk="order_id")
    t.merge_table("products", con.sql("FROM raw_products"), pk="product_id")
    # p=1.0: the tables are tiny, so any change would otherwise trip the 30% recompute threshold.
    return (t.source("orders", p=1.0).join(t.source("products", p=1.0), on="product_id")
              .select("s0.order_id, s1.category, s0.quantity").merge("line", pk="order_id"))


# ─── epochs and run state ────────────────────────────────────────────────────────


def test_a_first_run_bootstraps_and_commits_its_epoch(con):
    with trickle.run(con, pipeline="nightly", now=T0) as t:
        assert (t.f, t.previous_f, t.replay) == (T0, NEVER, False)
        _pipeline(t, con)
    assert _out(con) == _scratch(con)
    assert con.execute(f"SELECT last_f, pending_f FROM {RUN_TABLE}").fetchone() == (T0, None)
    with trickle.run(con, pipeline="nightly", now=T0 + timedelta(hours=1)) as t:
        assert t.previous_f == T0 and t.f == T0 + timedelta(hours=1)


def test_epochs_never_go_back(con):
    with trickle.run(con, now=T0):
        pass
    with trickle.run(con, now=T0 - timedelta(days=1)) as t:  # the clock stepped back
        assert t.f == T0 + timedelta(microseconds=1)


def test_pipelines_share_a_database_by_name(con):
    with trickle.run(con, pipeline="a", now=T0):
        pass
    with trickle.run(con, pipeline="b", now=T0) as b:
        assert b.previous_f == NEVER  # its own first run
        assert b.f > T0               # but still past every epoch the database has seen
    with trickle.run(con, pipeline="a", now=T0 + timedelta(hours=1)) as a:
        assert a.previous_f == T0


# ─── incremental runs ────────────────────────────────────────────────────────────


def test_a_later_run_is_incremental(con, monkeypatch):
    paths = _paths(monkeypatch)
    with trickle.run(con, now=T0) as t:
        _join(t, con)
    assert paths == ["comprehensive"]  # bootstrap
    paths.clear()
    con.execute("UPDATE raw_products SET category = 'Garden' WHERE product_id = 11")
    con.execute("INSERT INTO raw_orders VALUES (4, 11, 3)")
    with trickle.run(con, now=T0 + timedelta(hours=1)) as t:
        _join(t, con)
    assert paths == ["incremental"]
    assert sorted(con.sql("FROM line").fetchall()) == [(1, "Books", 2), (2, "Garden", 1), (3, "Books", 5),
                                                       (4, "Garden", 3)]
    paths.clear()
    with trickle.run(con, now=T0 + timedelta(hours=2)) as t:  # nothing changed
        out = _join(t, con)
    assert not out.was_changed() and paths == []


def test_a_plain_source_is_read_in_full(con, monkeypatch):
    """A table the engine didn't write has no change record, so whatever depends on it recomputes in full
    each run. That's correct, just not incremental."""
    paths = _paths(monkeypatch)
    for hour, price in ((0, 12.5), (1, 15.0)):
        con.execute(f"UPDATE raw_products SET unit_price = {price} WHERE product_id = 10")
        with trickle.run(con, now=T0 + timedelta(hours=hour)) as t:
            t.merge_table("orders", con.sql("FROM raw_orders"), pk="order_id")
            (t.source("orders").join(t.source("raw_products"), on="product_id")
               .select("s0.order_id, s0.quantity * s1.unit_price AS revenue").merge("line", pk="order_id"))
    assert paths == ["comprehensive", "comprehensive"]
    assert sorted(con.sql("FROM line").fetchall()) == [(1, 30), (2, 30), (3, 75)]


def test_the_readme_pipeline_matches_a_recompute_over_many_runs(con):
    rng = random.Random(7)
    for i in range(12):
        with trickle.run(con, pipeline="nightly", now=T0 + timedelta(hours=i)) as t:
            _pipeline(t, con)
        assert _out(con) == _scratch(con), f"run {i}"
        # The next snapshot: new orders, changed and deleted ones, a price change, a new product.
        con.execute(f"INSERT INTO raw_orders VALUES ({100 + i}, {rng.choice([10, 11, 12])}, {rng.randint(1, 9)})")
        con.execute(f"UPDATE raw_orders SET quantity = quantity + 1 WHERE order_id = {rng.choice([1, 2, 3])}")
        if i == 4:
            con.execute("DELETE FROM raw_orders WHERE order_id = 2")
        if i == 6:
            con.execute("INSERT INTO raw_products VALUES (12, 'Food', 4.00)")
        con.execute(f"UPDATE raw_products SET unit_price = unit_price + 1 WHERE product_id = {rng.choice([10, 11])}")


# ─── a failed run is replayed ────────────────────────────────────────────────────


def test_a_failed_run_is_replayed_at_the_same_epoch(con):
    with trickle.run(con, now=T0):
        pass  # an empty first run, so the next has a previous epoch
    with trickle.run(con, now=T0 + timedelta(hours=1)) as t:
        _pipeline(t, con)
    con.execute("UPDATE raw_orders SET quantity = 7 WHERE order_id = 1")
    with pytest.raises(RuntimeError), trickle.run(con, now=T0 + timedelta(hours=2)) as t:
        failed_f = t.f
        _pipeline(t, con)
        raise RuntimeError("the run fails after writing")
    assert con.execute(f"SELECT pending_f FROM {RUN_TABLE}").fetchone() == (failed_f,)

    # The input changes again before the retry; the replay must still give the right answer.
    con.execute("UPDATE raw_orders SET quantity = 9 WHERE order_id = 1")
    con.execute("INSERT INTO raw_orders VALUES (4, 11, 2)")
    con.execute("DELETE FROM raw_orders WHERE order_id = 2")
    with trickle.run(con, now=T0 + timedelta(hours=3)) as t:
        assert t.replay and t.f == failed_f and t.previous_f == T0 + timedelta(hours=1)
        _pipeline(t, con)
    assert _out(con) == _scratch(con)
    with trickle.run(con, now=T0 + timedelta(hours=4)) as t:
        assert not t.replay and t.previous_f == failed_f
        _pipeline(t, con)
    assert _out(con) == _scratch(con)


def test_read_delta_covers_the_pipelines_window(con):
    with trickle.run(con, pipeline="load", now=T0) as t:
        t.append_table("event", con.sql("SELECT 1 AS id"))
    with trickle.run(con, pipeline="load", now=T0 + timedelta(hours=1)) as t:
        t.append_table("event", con.sql("SELECT 2 AS id"))
    with trickle.run(con, pipeline="load", now=T0 + timedelta(hours=2)) as t:
        t.append_table("event", con.sql("SELECT 3 AS id"))
    # A consumer pipeline's first run reads everything; its second only what was appended since.
    with trickle.run(con, pipeline="consume", now=T0 + timedelta(hours=2, minutes=1)) as t:
        first = t.read_delta("event")
        assert first.is_full and sorted(first.upserts.fetchall()) == [(1,), (2,), (3,)]
    with trickle.run(con, pipeline="load", now=T0 + timedelta(hours=3)) as t:
        t.append_table("event", con.sql("SELECT 4 AS id"))
    with trickle.run(con, pipeline="consume", now=T0 + timedelta(hours=4)) as t:
        later = t.read_delta("event")
        assert not later.is_full and later.upserts.fetchall() == [(4,)]
        assert t.count_table("event") == 4
