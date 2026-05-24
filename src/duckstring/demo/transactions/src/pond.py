import random
import time
from datetime import date, timedelta

from duckstring import ripple

_PRODUCTS = 10
_STORES = 5
_BATCH = 5


@ripple
def ingest(pond):
    time.sleep(1)
    try:
        (next_id,) = pond.con.execute(
            'SELECT COALESCE(MAX(id), -1) + 1 FROM "transactions"."transaction"'
        ).fetchone()
        existing = pond.con.sql('SELECT * FROM "transactions"."transaction"')
    except Exception:
        next_id = 0
        existing = None

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
    vals = ", ".join(
        f"({r[0]}, DATE '{r[1]}', {r[2]}, {r[3]}, {r[4]})" for r in rows
    )
    new_data = pond.con.sql(
        f"SELECT * FROM (VALUES {vals}) t(id, created_at, product_id, quantity, store_id)"
    )

    if existing is not None:
        combined = pond.con.sql(
            "SELECT * FROM existing UNION ALL SELECT * FROM new_data"
        )
        pond.write_table("transaction", combined)
    else:
        pond.write_table("transaction", new_data)
