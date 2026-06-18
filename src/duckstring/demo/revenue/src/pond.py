"""revenue — a comprehensive merge Trickle aggregating the priced lines.

Reads the **clean current state** of ``priced.priced_line`` (a Trickle source — its system columns are
stripped on read), re-aggregates per product, and merges comprehensively: Duckstring diffs the new
totals against the prior ones so only the products whose revenue actually moved hit the changelog. An
aggregate recomputes fully each run (the win is the small delta *out*, not less compute in) — the honest
scope of Trickle. No ``time.sleep``: the full re-aggregation over the (large) priced lines is the work.
"""

from duckstring import trickle


@trickle(pk="product_id")
def by_product(pond):
    pond.read_table("priced.priced_line")  # registers the Source as the view `priced_line`
    totals = pond.con.sql("""
        SELECT
            product_id,
            ROUND(SUM(revenue), 2)  AS total_revenue,
            SUM(quantity)           AS units_sold,
            COUNT(*)                AS order_count
        FROM priced_line
        GROUP BY product_id
    """)
    pond.merge_table("revenue_by_product", totals)  # full current state → diffed to derive the delta out
