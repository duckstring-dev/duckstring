"""Join types maintained incrementally (left, semi, anti, right and full, including their unmatched rows), a
mixed left/inner star, a bushy (A⋈B)⋈(C⋈D) DAG, and the join guards. Each incremental result is checked
against a recompute over the sources' current state."""

from __future__ import annotations

import pytest
from trickle import agg
from trickle.builder import BuildError
from trickle_support import NEVER, at, merge, rows, star, ts


def _join(db, how, select, out, pk, f, pf):
    t = at(db, f, pf)
    (t.source("order_line", p=1.0).alias("o")  # p=1.0: exercise the incremental path, not the fallback
      .join(t.source("product", p=1.0).alias("p"), on="product_id", how=how)
      .select(select).merge(out, pk=pk))


def test_builder_left_join_keeps_unmatched_spine(db):
    ol, pr = star(db)

    def run(f, pf):
        _join(db, "left", "o.order_id, p.price", "j", "order_id", f, pf)

    ol([(10, "p1", 2), (11, "pX", 1)], ts(1))  # pX has no product row
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "j", "order_id, price") == [(10, 5), (11, None)]
    pr([("p1", 5), ("p2", 9), ("pX", 7)], ts(2))  # pX appears: 11 becomes matched
    run(ts(2), ts(1))
    assert rows(db, "j", "order_id, price") == [(10, 5), (11, 7)]
    pr([("p1", 50), ("p2", 9), ("pX", 7)], ts(3))
    run(ts(3), ts(2))
    assert rows(db, "j", "order_id, price") == [(10, 50), (11, 7)]
    ol([(10, "p1", 2), (11, "pX", 1), (12, "pZ", 1)], ts(4))  # a new unmatched spine row
    run(ts(4), ts(3))
    assert rows(db, "j", "order_id, price") == [(10, 50), (11, 7), (12, None)]


def test_builder_semi_join_filters_spine(db):
    ol, pr = star(db)

    def run(f, pf):
        _join(db, "semi", "o.order_id, o.qty", "j", "order_id", f, pf)

    ol([(10, "p1", 2), (11, "pX", 1)], ts(1))
    pr([("p1", 5)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "j", "order_id, qty") == [(10, 2)]
    pr([("p1", 5), ("pX", 7)], ts(2))  # pX appears: 11 enters
    run(ts(2), ts(1))
    assert rows(db, "j", "order_id, qty") == [(10, 2), (11, 1)]
    pr([("p1", 5)], ts(3))  # pX removed: 11 leaves
    run(ts(3), ts(2))
    assert rows(db, "j", "order_id, qty") == [(10, 2)]


def test_builder_anti_join_keeps_unmatched(db):
    ol, pr = star(db)

    def run(f, pf):
        _join(db, "anti", "o.order_id, o.qty", "j", "order_id", f, pf)

    ol([(10, "p1", 2), (11, "pX", 1)], ts(1))
    pr([("p1", 5), ("pX", 7)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "j", "order_id, qty") == []  # both match
    pr([("p1", 5)], ts(2))  # pX removed: 11 has no match
    run(ts(2), ts(1))
    assert rows(db, "j", "order_id, qty") == [(11, 1)]


def test_builder_right_and_full_outer_join_on_bootstrap(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "pX", 1)], ts(1))  # pX has no product; p2 has no order
    pr([("p1", 5), ("p2", 9)], ts(1))
    _join(db, "right", "p.product_id, o.order_id", "r", "product_id", ts(1), NEVER)
    assert rows(db, "r", "product_id, order_id") == [("p1", 10), ("p2", None)]
    _join(db, "full", "coalesce(o.product_id, p.product_id) AS pid, o.order_id, p.price", "fo", "pid", ts(1), NEVER)
    assert rows(db, "fo", "pid, order_id, price") == [("p1", 10, 5), ("p2", None, 9), ("pX", 11, None)]


def test_builder_right_join_incremental(db):
    """A right join keeps the B side's unmatched rows and tracks their first match, last match and new
    unmatched rows across runs."""
    ol, pr = star(db)

    def run(f, pf):
        _join(db, "right", "p.product_id, o.order_id", "r", "product_id", f, pf)

    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))  # p2 unmatched
    run(ts(1), NEVER)
    assert rows(db, "r", "product_id, order_id") == [("p1", 10), ("p2", None)]
    ol([(10, "p1", 2), (12, "p2", 1)], ts(2))     # p2 gains its first order
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(2))  # p3 appears unmatched
    run(ts(2), ts(1))
    assert rows(db, "r", "product_id, order_id") == [("p1", 10), ("p2", 12), ("p3", None)]
    ol([(10, "p1", 2)], ts(3))  # p2's only order removed: p2 unmatched again
    run(ts(3), ts(2))
    assert rows(db, "r", "product_id, order_id") == [("p1", 10), ("p2", None), ("p3", None)]


def test_builder_full_join_incremental_matches_comprehensive(db):
    ol, pr = star(db)
    select = "coalesce(o.product_id, p.product_id) AS pid, o.order_id, p.price"

    def truth():
        return sorted(db.sql("SELECT coalesce(o.product_id, p.product_id) AS pid, o.order_id, p.price "
                             "FROM order_line o FULL JOIN product p USING (product_id)").fetchall())

    ol([(10, "p1", 2), (11, "pX", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _join(db, "full", select, "fo", "pid", ts(1), NEVER)
    assert rows(db, "fo", "pid, order_id, price") == truth()
    # pX gains a product; p2 gains an order; p1's product is dropped (order 10 is now unmatched).
    ol([(10, "p1", 2), (11, "pX", 1), (12, "p2", 4)], ts(2))
    pr([("p2", 9), ("pX", 7)], ts(2))
    _join(db, "full", select, "fo", "pid", ts(2), ts(1))
    assert rows(db, "fo", "pid, order_id, price") == truth()


def test_builder_mixed_left_inner_star(db):
    def fact(state, f):
        merge(db, "fact", state, "order_id, product_id, store_id", f, "order_id")

    def prod(state, f):
        merge(db, "prod", state, "product_id, price", f, "product_id")

    def store(state, f):
        merge(db, "store", state, "store_id, region", f, "store_id", types="VARCHAR, VARCHAR")

    def run(f, pf):
        t = at(db, f, pf)
        (t.source("fact", p=1.0).alias("f")
          .join(t.source("prod", p=1.0).alias("p"), on="product_id", how="left")
          .join(t.source("store", p=1.0).alias("s"), on="store_id", how="inner")
          .select("f.order_id, p.price, s.region")
          .merge("m", pk="order_id"))

    fact([(10, "p1", "sA"), (11, "pX", "sA")], ts(1))  # pX unmatched in prod (left: NULL)
    prod([("p1", 5)], ts(1))
    store([("sA", "north")], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "m", "order_id, price, region") == [(10, 5, "north"), (11, None, "north")]
    store([("sA", "south")], ts(2))  # an inner-dim change: both orders move region
    run(ts(2), ts(1))
    assert rows(db, "m", "order_id, price, region") == [(10, 5, "south"), (11, None, "south")]
    prod([("p1", 5), ("pX", 7)], ts(3))  # the left dim gains pX: order 11 is now priced
    run(ts(3), ts(2))
    assert rows(db, "m", "order_id, price, region") == [(10, 5, "south"), (11, 7, "south")]
    store([], ts(4))  # store emptied: the inner join drops everything
    run(ts(4), ts(3))
    assert rows(db, "m", "order_id, price, region") == []


def test_builder_bushy_join_matches_comprehensive(db):
    """(A⋈B)⋈(C⋈D): a bushy DAG with composed operands on both sides of the top join. The incremental result
    equals a recompute across a run that changes a leaf in each subtree."""

    def run(f, pf):
        t = at(db, f, pf)
        ab = t.source("orders", p=1.0).alias("o").join(t.source("customers", p=1.0).alias("cu"), on="cust_id")
        cd = t.source("products", p=1.0).alias("pr").join(t.source("categories", p=1.0).alias("g"), on="cat_id")
        ab.join(cd, on="prod_id").select("o.order_id, cu.region, g.tax").merge("res", pk="order_id")

    def leaves(orders, customers, f, categories=None, products=None):
        merge(db, "orders", orders, "order_id, cust_id, prod_id", f, "order_id")
        merge(db, "customers", customers, "cust_id, region", f, "cust_id")
        if products is not None:
            merge(db, "products", products, "prod_id, cat_id", f, "prod_id")
        if categories is not None:
            merge(db, "categories", categories, "cat_id, tax", f, "cat_id")

    leaves([(1, "cA", "pX"), (2, "cB", "pY")], [("cA", "north"), ("cB", "south")], ts(1),
           categories=[("books", 10), ("food", 5)], products=[("pX", "books"), ("pY", "food")])
    run(ts(1), NEVER)
    assert rows(db, "res", "order_id, region, tax") == [(1, "north", 10), (2, "south", 5)]
    # A change in each subtree at once: a new order (A), a region update (B), a tax update (D).
    leaves([(1, "cA", "pX"), (2, "cB", "pY"), (3, "cA", "pY")], [("cA", "NORTH"), ("cB", "south")], ts(2),
           categories=[("books", 12), ("food", 5)])
    run(ts(2), ts(1))
    truth = sorted(db.sql("SELECT orders.order_id, customers.region, categories.tax FROM orders "
                          "JOIN customers USING (cust_id) JOIN products USING (prod_id) "
                          "JOIN categories USING (cat_id)").fetchall())
    assert rows(db, "res", "order_id, region, tax") == truth == [(1, "NORTH", 12), (2, "south", 5),
                                                                  (3, "NORTH", 5)]


def test_builder_join_guards(db):
    t = at(db, ts(1))
    with pytest.raises(BuildError, match="how"):
        t.source("a").join(t.source("b"), on="k", how="cross")
    # A right or full outer join can sit anywhere in a DAG.
    t.source("a").join(t.source("b"), on="k", how="full").join(t.source("c"), on="k")
    with pytest.raises(BuildError, match="operand"):  # an aggregate result can't be a join operand
        t.source("a").join(t.source("b").aggregate(by="k", n=agg.count()), on="k")
