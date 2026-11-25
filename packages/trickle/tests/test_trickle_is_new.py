"""``is_new``: whether a table holds nothing written before this run, the check hand-written incremental code
makes beside ``Delta.is_full`` (a new or deleted output is filled from its sources' full state)."""

from __future__ import annotations

import duckdb
import pytest
from trickle_support import NEVER, T, at, merge, rows, ts


def test_a_merge_table_is_new_until_a_run_after_its_first_write():
    con = duckdb.connect()
    assert T.is_new(con, "m", ts(1))
    merge(con, "m", [(1, "a")], "id, v", ts(1), "id")
    assert T.is_new(con, "m", ts(1))  # a retry of the run that first wrote it
    assert not T.is_new(con, "m", ts(2))
    merge(con, "m", [(1, "b")], "id, v", ts(2), "id")
    assert not T.is_new(con, "m", ts(2))  # a retry of a later run
    T.drop_table(con, "m")
    assert T.is_new(con, "m", ts(3))


def test_an_append_table_is_new_until_a_run_after_its_first_write():
    con = duckdb.connect()
    assert T.is_new(con, "a", ts(1))
    T.append_table(con, "a", con.sql("SELECT 1 AS id"), ts(1), ("id",))
    assert T.is_new(con, "a", ts(1))
    assert not T.is_new(con, "a", ts(2))
    T.drop_table(con, "a")
    assert T.is_new(con, "a", ts(2))


def test_a_plain_table_is_new_while_missing():
    con = duckdb.connect()
    assert T.is_new(con, "p", ts(1))
    con.execute("CREATE TABLE p AS SELECT 1 AS id")
    assert not T.is_new(con, "p", ts(1))


def _big_orders(t):
    """Hand-written incremental code: the orders of 2 or more, copied from the append Source ``orders``."""
    d = t.read_delta("orders")
    if d.is_full or t.is_new("big_orders"):
        t.merge_table("big_orders", t.read_table("orders").filter("qty >= 2"), pk="id")
    else:
        t.apply_zset("big_orders", d.zset.filter("qty >= 2"), pk="id")


def _order(con, f, k, qty):
    T.append_table(con, "orders", con.sql(f"SELECT {int(f.hour)} AS id, '{k}' AS k, {qty} AS qty"), f, ("id",))


def test_an_output_added_to_a_running_pipeline_is_filled_from_the_full_source():
    con = duckdb.connect()
    for h, k, q in ((1, "a", 1), (2, "a", 2), (3, "b", 4)):
        _order(con, ts(h), k, q)
    # "big_orders" is added at run 3: its window holds only run 3's order, but it fills from everything.
    _big_orders(at(con, ts(3), ts(2)))
    assert rows(con, "big_orders", "k, qty") == [("a", 2), ("b", 4)]
    _order(con, ts(4), "a", 8)
    _big_orders(at(con, ts(4), ts(3)))
    assert rows(con, "big_orders", "k, qty") == [("a", 2), ("a", 8), ("b", 4)]


def test_a_retry_of_the_run_that_first_wrote_an_output_fills_it_again():
    con = duckdb.connect()
    for h, k, q in ((1, "a", 1), (2, "a", 2), (3, "b", 4)):
        _order(con, ts(h), k, q)

    def failing(t):
        _big_orders(t)
        raise RuntimeError("a later step fails")

    with pytest.raises(RuntimeError):
        failing(at(con, ts(3), ts(2)))
    _big_orders(at(con, ts(3), ts(2)))  # the retry: same window; the output is still new
    assert rows(con, "big_orders", "k, qty") == [("a", 2), ("b", 4)]


def test_a_deleted_output_is_filled_again_from_the_full_source():
    con = duckdb.connect()
    _order(con, ts(1), "a", 3)
    _big_orders(at(con, ts(1), NEVER))
    _order(con, ts(2), "b", 2)
    T.drop_table(con, "big_orders")
    _big_orders(at(con, ts(2), ts(1)))
    assert rows(con, "big_orders", "k, qty") == [("a", 3), ("b", 2)]
