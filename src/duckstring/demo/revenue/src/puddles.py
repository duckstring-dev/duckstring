"""Puddle — a Trickle-shaped snapshot of revenue's Source (``priced.priced_line``), so
``duckstring pond run`` aggregates locally without deploying the whole chain. See priced/src/puddles.py
for the pattern (the Trickle layout the data plane writes, not a plain overwrite table)."""

from datetime import datetime, timezone

from duckstring import puddle

_F = datetime(2026, 1, 1, tzinfo=timezone.utc)

_PRICED = [  # order_id, product_id, quantity, unit_price, revenue
    (0, 1, 2, 1299.00, 2598.00),
    (1, 2, 1, 89.99, 89.99),
    (2, 1, 3, 1299.00, 3897.00),
    (3, 3, 1, 79.99, 79.99),
]


@puddle("priced")
def priced(p):
    import duckdb

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane

    con = duckdb.connect()
    try:
        vals = ", ".join(f"({o}, {pid}, {q}, {up}, {rev})" for o, pid, q, up, rev in _PRICED)
        rel = con.sql(
            f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, quantity, unit_price, revenue)"
        )
        trickle_io.merge_table(con, "priced_line", rel, _F, ("order_id",))
        ParquetDataPlane().export(con, p.path)
    finally:
        con.close()
