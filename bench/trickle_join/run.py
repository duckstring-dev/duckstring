"""The builder's join delta on `priced` (orders joined to a product catalogue) at 10x the demo: one bootstrap,
then one incremental run per variant, timed. Run from the repo root: `uv run python bench/trickle_join/run.py`.

`HOW=left` (or any join type that keeps order_id unique) measures that join instead of the inner one.

Variants: the builder as it is (`default`), with `key_filter=False`, with `ivm=False` (recompute the whole output
and diff it), and a hand-written per-term delta in plain SQL as a reference. Each variant's changelog rows at
the run are printed so the variants can be checked against each other."""

import os
import sys
import time
from datetime import datetime, timezone

import duckdb
import trickle
from trickle import NEVER

HOW = os.environ.get("HOW", "inner")
ORDERS, PRODUCTS, REPRICED = 15_000_000, 1_000_000, 1000
SEL = ("s0.order_id, s0.product_id, s0.quantity, s1.unit_price, "
       "CAST(round(s0.quantity * s1.unit_price, 2) AS DECIMAL(14,2)) AS revenue")


def t(h):
    return datetime(2026, 6, 16, h, tzinfo=timezone.utc)


def sources(con, f, pf, first, n, seed):
    e = trickle.at(con, f, pf)
    e.append_table("order_line", con.sql(f"""
        SELECT {first} + i AS order_id, (hash(i + {first}) % {PRODUCTS})::INT + 1 AS product_id,
               (hash(i * 7 + {first}) % 8)::INT + 1 AS quantity FROM range({n}) t(i)"""),
        pk="order_id", fail_on_conflict=False)
    drift = "" if seed is None else (
        f"CASE WHEN hash(product_id + {seed}) % {PRODUCTS // REPRICED} = 0 THEN 1.1 ELSE 1.0 END *")
    e.merge_table("product", con.sql(f"""
        SELECT product_id, CAST(round({drift} (5.0 + (product_id * 37 % 2000) * 0.5), 2) AS DECIMAL(12,2)) AS unit_price
        FROM (SELECT i + 1 AS product_id FROM range({PRODUCTS}) t(i) WHERE i % 10 <> 0)"""), pk="product_id")


def priced(e, **kw):
    return (e.source("order_line").join(e.source("product"), on="product_id", how=HOW).select(SEL)
            .merge("priced_line", pk="order_id", **kw))


def per_term(e):
    """δO = δA ⋈ B' + A' ⋈ δB − δA ⋈ δB, written out by hand."""
    con = e.con
    e.read_delta("order_line").zset.create_view("da", replace=True)
    e.read_delta("product").zset.create_view("db", replace=True)
    e.read_table("order_line").create_view("a_new", replace=True)
    e.read_table("product").create_view("b_new", replace=True)
    cols = ("a.order_id, a.product_id, a.quantity, b.unit_price, "
            "CAST(round(a.quantity * b.unit_price, 2) AS DECIMAL(14,2)) AS revenue")
    return e.apply_zset("priced_line", con.sql(f"""
        SELECT {cols}, a._duckstring_d AS _duckstring_d FROM da a JOIN b_new b USING (product_id)
        UNION ALL SELECT {cols}, b._duckstring_d FROM a_new a JOIN db b USING (product_id)
        UNION ALL SELECT {cols}, -(a._duckstring_d * b._duckstring_d) FROM da a JOIN db b USING (product_id)"""),
        pk="order_id")


def main(batch):
    con = duckdb.connect()
    sources(con, t(1), NEVER, 0, ORDERS, None)
    priced(trickle.at(con, t(1), NEVER))
    sources(con, t(2), t(1), ORDERS, batch, 42)
    variants = [("default", lambda e: priced(e)), ("key_filter=False", lambda e: priced(e, key_filter=False)),
                ("ivm=False", lambda e: priced(e, ivm=False))]
    if HOW == "inner":
        variants.append(("hand-written per-term", per_term))
    for label, fn in variants:
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            fn(trickle.at(con, t(2), t(1)))
            times.append(time.perf_counter() - t0)
            out = con.sql(f"SELECT count(*), sum(_duckstring_d) FROM priced_line__changelog "
                          f"WHERE _duckstring_f = '{t(2).isoformat()}'").fetchone()
            con.execute(f"DELETE FROM priced_line__changelog WHERE _duckstring_f = '{t(2).isoformat()}'")
        print(f"{HOW:5s} batch={batch:>7}  {label:24s} {sorted(times)[1] * 1000:6.0f} ms   changelog rows, net: {out}")


if __name__ == "__main__":
    for b in map(int, sys.argv[1:] or ["10000", "250000"]):
        main(b)
