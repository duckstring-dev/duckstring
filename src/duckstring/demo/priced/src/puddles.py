"""Puddles — Trickle-shaped local snapshots of priced's Sources.

A plain Ripple puddle is one overwrite table; a **Trickle** source carries history + a ``_trickle.json``
mode/PK sidecar so ``read_delta`` can resolve it. These build that layout directly through the data
plane (an append source for ``orders``, a merge source for ``catalog``), so ``duckstring pond run``
exercises the incremental builder locally. Re-run with edited values to watch a delta propagate.
"""

from datetime import datetime, timezone

from duckstring import puddle

_F = datetime(2026, 1, 1, tzinfo=timezone.utc)  # one snapshot freshness for the seed

_ORDERS = [(0, 1, 2), (1, 2, 1), (2, 1, 3), (3, 3, 1)]  # order_id, product_id, quantity
_PRODUCTS = [  # product_id, name, category, unit_price
    (1, "Laptop Pro", "Electronics", 1299.00),
    (2, "Wireless Earbuds", "Electronics", 89.99),
    (3, "Running Shoes", "Clothing", 79.99),
]


def _publish(p, build) -> None:
    """Run ``build(con)`` (which calls the Trickle write API) then publish that registry to the puddle's
    Source directory, sidecar and all — the catchment-root layout ``read_delta`` reads unchanged."""
    import duckdb

    from duckstring.dataplane import ParquetDataPlane

    con = duckdb.connect()
    try:
        build(con)
        ParquetDataPlane().export(con, p.path)
    finally:
        con.close()


@puddle("orders")
def orders(p):
    from duckstring import trickle_io

    def build(con):
        vals = ", ".join(f"({o}, {pid}, {q})" for o, pid, q in _ORDERS)
        rel = con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, quantity)")
        trickle_io.append_table(con, "order_line", rel, _F, ("order_id",))

    _publish(p, build)


@puddle("catalog")
def catalog(p):
    from duckstring import trickle_io

    def build(con):
        vals = ", ".join(f"({pid}, '{n}', '{c}', {pr})" for pid, n, c, pr in _PRODUCTS)
        rel = con.sql(f"SELECT * FROM (VALUES {vals}) v(product_id, name, category, unit_price)")
        trickle_io.merge_table(con, "product", rel, _F, ("product_id",))

    _publish(p, build)
