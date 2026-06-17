"""catalog — a merge Trickle (the history-preserving upsert path).

Each run emits the **complete** current catalogue; ``merge_table(comprehensive=True)`` diffs it against
the prior state to derive inserts/updates/deletes automatically (the safe default — no enumerate-every-
change obligation). So when a price drifts between runs, the change shows up as a single ``upsert`` in
the changelog; a discontinued product shows up as a ``delete`` — and a downstream Trickle re-prices only
the affected order lines, never the whole catalogue.

The clean *main* table is the current catalogue (one row per product, no tombstones); the ``__changelog``
companion is the CDC stream a delta read consumes.
"""

import os
import random
import time

from duckstring import trickle

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


@trickle(pk="product_id")
def ingest(pond):
    time.sleep(2 * float(os.environ.get("DUCKSTRING_SLEEP_MULTIPLIER", "1.0")))
    # Jitter a couple of prices each run so the comprehensive diff has updates to detect.
    catalog = [
        (pid, name, cat, round(price * random.choice([1.0, 1.0, 1.0, 0.9, 1.1]), 2))
        for pid, name, cat, price in _CATALOG
    ]
    vals = ", ".join(f"({r[0]}, '{r[1]}', '{r[2]}', {r[3]:.2f})" for r in catalog)
    state = pond.con.sql(f"SELECT * FROM (VALUES {vals}) t(product_id, name, category, unit_price)")
    pond.merge_table("product", state)  # comprehensive (default): Duckstring derives the CDC
