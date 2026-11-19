"""Puddle: a local stand-in for revenue's Source (``priced.priced_line``, a merge Trickle), so
``duckstring pond run`` aggregates locally without deploying the whole chain. The step changes one line and
adds another, so ``pond run --incremental`` updates the aggregate from the change alone."""

from duckstring import puddle

_PRICED = [  # order_id, product_id, quantity, unit_price, revenue
    (0, 1, 2, 1299.00, 2598.00),
    (1, 2, 1, 89.99, 89.99),
    (2, 1, 3, 1299.00, 3897.00),
    (3, 3, 1, 79.99, 79.99),
]


def _priced(p, rows):
    vals = ", ".join(f"({o}, {pid}, {q}, {up}, {rev})" for o, pid, q, up, rev in rows)
    return p.con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, quantity, unit_price, revenue)")


@puddle("priced.priced_line")
def priced(p):
    p.merge_table(_priced(p, _PRICED), pk="order_id")


@puddle.step("priced.priced_line")
def more(p):
    rows = [*_PRICED[1:], (0, 1, 1, 1299.00, 1299.00)]  # order 0 now one laptop
    rows += [(3 + i, 2, 1, 89.99, 89.99) for i in range(1, p.step + 1)]  # and one more earbuds order a step
    p.merge_table(_priced(p, rows), pk="order_id")
