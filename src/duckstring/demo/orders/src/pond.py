"""orders — an append Trickle (the insert-only history path).

Each run appends a fresh batch of order lines stamped with this run's freshness. Because identity is
unique by construction (a new ``order_id`` per line), this is the trust-the-writer fast path:
``append_table`` does no diff and keeps no main/changelog — the single history table *is* the full read
and the delta source. A downstream Trickle reads just the new lines via ``read_delta``.
"""

import os
import random
import time
from datetime import date, timedelta

from duckstring import trickle

_PRODUCTS = 10
_STORES = 5
_BATCH = 5


@trickle(pk="order_id")
def ingest(pond):
    time.sleep(1 * float(os.environ.get("DUCKSTRING_SLEEP_MULTIPLIER", "1.0")))
    # Continue the id sequence across runs — the history table persists in the registry.
    try:
        (next_id,) = pond.con.execute('SELECT COALESCE(MAX(order_id), -1) + 1 FROM order_line').fetchone()
    except Exception:
        next_id = 0

    today = date.today()
    rows = [
        (
            next_id + i,
            (today - timedelta(days=random.randint(0, 89))).isoformat(),
            random.randint(1, _PRODUCTS),
            random.randint(1, 8),
            random.randint(1, _STORES),
        )
        for i in range(_BATCH)
    ]
    vals = ", ".join(f"({r[0]}, DATE '{r[1]}', {r[2]}, {r[3]}, {r[4]})" for r in rows)
    batch = pond.con.sql(
        f"SELECT * FROM (VALUES {vals}) t(order_id, ordered_at, product_id, quantity, store_id)"
    )
    pond.append_table("order_line", batch)  # insert-only; each row stamped with pond.f
