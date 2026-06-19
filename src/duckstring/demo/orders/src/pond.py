"""orders — an append Trickle (the insert-only history path).

Each run appends a fresh batch of order lines stamped with this run's freshness. Because identity is
unique by construction (a new ``order_id`` per line), this is the trust-the-writer fast path:
``append_table`` does no diff and keeps no main/changelog — the single history table *is* the full read
and the delta source. A downstream Trickle reads just the new lines via ``read_delta``.

The data is **generated**, not shipped — the first run bootstraps a large history (``_BOOTSTRAP`` rows,
~50 MB once priced) so the chain has real volume; every run after that appends a small ``_BATCH``. That
contrast — a big standing table, a tiny per-run delta — is the whole point of the incremental demo, so
the sizes are deliberately large. Override them with the ``DUCKSTRING_DEMO_*`` env vars (the test suite
shrinks them to keep the e2e runs quick); no ``time.sleep`` — the work here is the data, not a stub.
"""

import os
from datetime import date

from duckstring import ripple

_PRODUCTS = int(os.environ.get("DUCKSTRING_DEMO_PRODUCTS", "100000"))
_STORES = 50
_BOOTSTRAP = int(os.environ.get("DUCKSTRING_DEMO_ORDERS", "1500000"))  # first-run history (~50 MB priced)
_BATCH = int(os.environ.get("DUCKSTRING_DEMO_BATCH", "25000"))  # appended every subsequent run (~500 KB)


@ripple
def ingest(pond):
    # Continue the id sequence across runs — the history table persists in the registry. The first run
    # (no table yet) lays down the big bootstrap; every run after it appends a small batch.
    try:
        (next_id,) = pond.con.execute("SELECT COALESCE(MAX(order_id), -1) + 1 FROM order_line").fetchone()
    except Exception:
        next_id = 0

    n = _BOOTSTRAP if next_id == 0 else _BATCH
    # Generate the batch in SQL (a Python VALUES list of a million rows is hopeless): random products,
    # quantities, stores and a date within the last quarter.
    batch = pond.con.sql(
        f"""
        SELECT
          {next_id} + i                                                          AS order_id,
          DATE '{date.today().isoformat()}'
            - CAST(floor(random() * 90) AS INTEGER) * INTERVAL '1 day'           AS ordered_at,
          CAST(floor(random() * {_PRODUCTS}) + 1 AS INTEGER)                     AS product_id,
          CAST(floor(random() * 8) + 1 AS INTEGER)                              AS quantity,
          CAST(floor(random() * {_STORES}) + 1 AS INTEGER)                      AS store_id
        FROM range({n}) AS t(i)
        """
    )
    # Insert-only, each row stamped with pond.f. order_id is unique by construction (the fast path), so we
    # declare it as the key (pk=) but skip validate_pk — no need to pay the per-write uniqueness check.
    pond.append_table("order_line", batch, pk="order_id")
