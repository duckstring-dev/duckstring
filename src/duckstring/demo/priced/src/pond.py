"""priced — the ``pond.trickle(...)`` builder: incremental star enrichment.

Joins the ``orders`` order-line stream to the ``catalog`` product dimension. The builder records the
join graph and walks it: it reads each Source's delta, propagates the affected order keys along the
edge (a new order line, *or* an order line whose product's price changed), recomputes only that slice,
and merges it. Because it sees the whole graph it can't forget the price-change edge — no silent
under-merge. The output is itself a merge Trickle (clean main + changelog) for ``revenue`` to consume.
"""

from duckstring import trickle


@trickle(pk="order_id")
def priced_line(pond):
    (
        pond.trickle("orders.order_line")
        .join(pond.trickle("catalog.product"), on="product_id")
        .select(
            "s0.order_id, s0.product_id, s0.quantity, s1.unit_price, "
            "round(s0.quantity * s1.unit_price, 2) AS revenue"
        )
        .merge("priced_line")
    )
