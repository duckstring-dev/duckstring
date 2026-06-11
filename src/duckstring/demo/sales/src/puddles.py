"""Puddles — local snapshots of this Pond's Sources, for testing before deployment.

    duckstring pond hydrate
    duckstring pond run
"""

import random
from datetime import date, timedelta

from duckstring import puddle

_CATALOG = [
    (1, "Laptop Pro", "Electronics", 1299.00),
    (2, "Wireless Earbuds", "Electronics", 89.99),
    (3, "Running Shoes", "Clothing", 79.99),
    (4, "Winter Jacket", "Clothing", 149.99),
    (5, "Espresso Beans", "Food", 14.99),
    (6, "Olive Oil", "Food", 11.49),
    (7, "Desk Lamp", "Home", 34.99),
    (8, "Throw Pillow", "Home", 24.99),
    (9, "Sci-Fi Novel", "Books", 16.99),
    (10, "Cookbook", "Books", 22.99),
]


@puddle("transactions.transaction")
def transactions(p):
    rng = random.Random(42)  # deterministic — the same snapshot every hydrate
    today = date.today()
    rows = [
        (
            i,
            (today - timedelta(days=rng.randint(0, 89))).isoformat(),
            rng.randint(1, len(_CATALOG)),
            rng.randint(1, 8),
            rng.randint(1, 5),
        )
        for i in range(50)
    ]
    vals = ", ".join(f"({r[0]}, DATE '{r[1]}', {r[2]}, {r[3]}, {r[4]})" for r in rows)
    return p.con.sql(f"SELECT * FROM (VALUES {vals}) t(id, created_at, product_id, quantity, store_id)")


@puddle("products.product")
def products(p):
    vals = ", ".join(f"({r[0]}, '{r[1]}', '{r[2]}', {r[3]:.2f})" for r in _CATALOG)
    return p.con.sql(f"SELECT * FROM (VALUES {vals}) t(id, name, category, unit_price)")
