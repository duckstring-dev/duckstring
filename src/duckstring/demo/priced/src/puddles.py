"""Puddles: local stand-ins for priced's Sources.

``orders`` is an append Trickle and ``catalog`` a merge Trickle, so these write through the handle's matching
methods: the Puddles carry the history incremental reads need, and ``duckstring pond run`` exercises the
builder locally. The steps give ``pond run --incremental`` a next window: new orders, and a price change.
"""

from duckstring import puddle

_ORDERS = [(0, 1, 2), (1, 2, 1), (2, 1, 3), (3, 3, 1)]  # order_id, product_id, quantity
_PRODUCTS = [  # product_id, name, category, unit_price
    (1, "Laptop Pro", "Electronics", 1299.00),
    (2, "Wireless Earbuds", "Electronics", 89.99),
    (3, "Running Shoes", "Clothing", 79.99),
]


def _orders(p, rows):
    vals = ", ".join(f"({o}, {pid}, {q})" for o, pid, q in rows)
    return p.con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, quantity)")


def _products(p, rows):
    vals = ", ".join(f"({pid}, '{n}', '{c}', {pr})" for pid, n, c, pr in rows)
    return p.con.sql(f"SELECT * FROM (VALUES {vals}) v(product_id, name, category, unit_price)")


@puddle("orders.order_line")
def orders(p):
    p.append_table(_orders(p, _ORDERS), pk="order_id")


@puddle("catalog.product")
def catalog(p):
    p.merge_table(_products(p, _PRODUCTS), pk="product_id")


@puddle.step("orders.order_line")
def more_orders(p):
    first = len(_ORDERS) + (p.step - 1) * 2  # two new orders a step, numbered on from the last
    p.append_table(_orders(p, [(first, 2, 1), (first + 1, 3, 2)]), pk="order_id")


@puddle.step("catalog.product")
def reprice(p):
    laptop = round(1299.00 - 50 * p.step, 2)  # the laptop gets cheaper each step
    p.merge_table(_products(p, [(1, "Laptop Pro", "Electronics", laptop), *_PRODUCTS[1:]]), pk="product_id")
