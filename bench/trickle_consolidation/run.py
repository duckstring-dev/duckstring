"""What consolidation costs on the builder's read path, and whether a join could skip it.

A merge Trickle `catalog` of 1M products, bootstrapped at run 0, then R runs that each reprice ~1,000 products
(a -1 of the old row and a +1 of the new one per product). Its changelog holds the whole history until a
checkpoint folds it into a base (Duckstring checkpoints a table only once it passes 256 MiB, so a table of
this size never has one in practice). Measured, in memory, median of 5:

1. reading the current state (the reconstruct: latest-per-key over base + changelog), no base vs a base;
2. consolidating a change window (what read_delta does), 2k and 100k rows;
3. the join term `ΔA ⋈ B′` for 10k new order lines, with B′ produced three ways, each followed by the final
   consolidation of the output: consolidated (as now); base + the consolidated changelog since the base,
   unioned unconsolidated; base + the raw changelog.

Run from the repo root: `uv run python bench/trickle_consolidation/run.py` (`WIDE=1`: a 12-column catalog with
strings, carried through the join)."""

import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import duckdb
from trickle import io as T

PRODUCTS = 1_000_000
WIDE = os.environ.get("WIDE") == "1"
# The columns besides product_id and price, as SQL over the product id `i`.
EXTRA = (", 'Product ' || i AS name, ['Electronics','Clothing','Food','Home','Books'][i % 5 + 1] AS category, "
         "'A description of product ' || i || ' with some words in it' AS description, (i % 97)::INT AS supplier_id, "
         "(i % 13) * 1.5 AS weight_kg, i % 2 = 0 AS active, DATE '2020-01-01' + (i % 1500)::INT AS launched, "
         "'SKU-' || lpad(i::VARCHAR, 8, '0') AS sku, (i % 7)::INT AS warehouse, (i % 1000) * 0.01 AS margin") if WIDE else ""
COLS = ("name, category, description, supplier_id, weight_kg, active, launched, sku, warehouse, margin, ") if WIDE else ""
T0 = datetime(2026, 6, 16, tzinfo=timezone.utc)


def f(r):
    return T0 + timedelta(hours=r)


def build(con, runs):
    """catalog with `runs` repricing runs after the bootstrap. Written straight into the changelog (the shape
    merge_table would leave), so a long history is quick to set up."""
    T.apply_zset(con, "catalog", con.sql(
        f"SELECT i AS product_id, (i % 2000) * 0.5 AS price{EXTRA}, 1::BIGINT AS _duckstring_d FROM range({PRODUCTS}) t(i)"),
        f(0), ("product_id",))
    if runs:
        extra = (EXTRA.replace(' i ', ' product_id ').replace('i %', 'product_id %').replace("|| i", "|| product_id")
                 .replace("(i ", "(product_id ").replace("i::", "product_id::"))
        at = f"TIMESTAMPTZ '{f(0).isoformat()}' + r * INTERVAL 1 HOUR"
        # Each run reprices products with hash(p, r) % 1000 = 0; v = how many times p was repriced before.
        con.execute(f"""
            CREATE TEMP TABLE ev AS
            SELECT p AS product_id, r, row_number() OVER (PARTITION BY p ORDER BY r) AS v
            FROM range({PRODUCTS}) a(p), range(1, {runs + 1}) b(r) WHERE hash(p * 7919 + r) % 1000 = 0""")
        con.execute(f"""
            INSERT INTO catalog__changelog BY NAME
            SELECT product_id, (product_id % 2000) * 0.5 + (v - 1) AS price{extra},
              -1 AS _duckstring_d, {at} AS _duckstring_f FROM ev
            UNION ALL BY NAME
            SELECT product_id, (product_id % 2000) * 0.5 + v AS price{extra},
              1 AS _duckstring_d, {at} AS _duckstring_f FROM ev""")
    T.refresh_current_view(con, "catalog")


def timed(con, sql, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts) * 1000


def main():
    print(f"{'history':>16}  {'base':>4}  {'clog rows':>10}  {'read state':>10}  {'B′ consol.':>10}  "
          f"{'base+consol.':>12}  {'base+raw':>8}")
    for runs in (0, 50, 250):
        for with_base in (False, True):
            con = duckdb.connect()
            build(con, runs)
            if with_base and runs:
                T.checkpoint(con, "catalog", f(runs - 1))  # fold all but the last run into a base
            elif with_base:
                continue
            # 10k new order lines, keys spread over every product.
            con.execute(f"CREATE TEMP TABLE da AS SELECT i AS order_id, (hash(i) % {PRODUCTS})::INT AS product_id, "
                        "1::BIGINT AS d FROM range(10000) t(i)")
            recon = T._reconstruct_sql_for(con, "catalog")
            fb = T._f_base(con, "catalog")
            has_base = T._table_exists(con, "catalog__base")
            base = (f"SELECT product_id, price, {COLS}1::BIGINT AS w FROM catalog__base" if has_base
                    else f"SELECT product_id, price, {COLS}1::BIGINT AS w FROM catalog__changelog WHERE false")
            above = f"_duckstring_f > TIMESTAMPTZ '{fb.isoformat()}'" if fb else "true"
            clog = f"SELECT product_id, price, {COLS}_duckstring_d AS w FROM catalog__changelog WHERE {above}"
            clog_consol = f"SELECT product_id, price, {COLS}sum(w)::BIGINT AS w FROM ({clog}) GROUP BY ALL HAVING sum(w) <> 0"
            n_clog = con.execute(f"SELECT count(*) FROM ({clog})").fetchone()[0]

            def term(b):  # ΔA ⋈ B′, consolidated, as the root does
                return (f"SELECT count(*) FROM (SELECT a.order_id, b.* EXCLUDE (w), sum(a.d * b.w) AS w "
                        f"FROM da a JOIN ({b}) b USING (product_id) GROUP BY ALL HAVING sum(a.d * b.w) <> 0)")

            read_state = timed(con, f"SELECT count(*), sum(price) FROM ({recon})")
            consol = timed(con, term(f"SELECT * EXCLUDE (_duckstring_f), 1::BIGINT AS w FROM ({recon})"))
            base_consol = timed(con, term(f"{base} UNION ALL BY NAME {clog_consol}"))
            base_raw = timed(con, term(f"{base} UNION ALL BY NAME {clog}"))
            label = f"bootstrap+{runs}"
            print(f"{label:>16}  {'yes' if with_base else 'no':>4}  {n_clog:>10,}  {read_state:>8.0f}ms  {consol:>8.0f}ms  "
                  f"{base_consol:>10.0f}ms  {base_raw:>6.0f}ms")
            # sanity: all three give the same consolidated result
            res = [con.execute(term(b)).fetchone()[0] for b in (
                f"SELECT * EXCLUDE (_duckstring_f), 1::BIGINT AS w FROM ({recon})",
                f"{base} UNION ALL BY NAME {clog_consol}",
                f"{base} UNION ALL BY NAME {clog}",
            )]
            assert len(set(res)) == 1, res
            if runs == 250 and not with_base:
                for win in (1, 50):
                    since = f"_duckstring_f > TIMESTAMPTZ '{f(runs - win).isoformat()}'"
                    w_sql = (f"SELECT * EXCLUDE (_duckstring_d, _duckstring_f), sum(_duckstring_d) FROM catalog__changelog "
                             f"WHERE {since} GROUP BY ALL HAVING sum(_duckstring_d) <> 0")
                    rows = con.execute(f"SELECT count(*) FROM catalog__changelog WHERE {since}").fetchone()[0]
                    ms = timed(con, f"SELECT count(*) FROM ({w_sql})")
                    print(f"{'':>16}  change window of {win} run(s): {rows:,} rows, consolidated in {ms:.1f}ms")


if __name__ == "__main__":
    main()
