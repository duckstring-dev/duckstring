"""The egress worker — delivers published Pond output to a Spout's external destination.

One async task in the Catchment process (outbound I/O, like the duct poller). It is a **reconciliation
loop**: each pass it asks the Driver for Spouts whose Pond has published past their watermark
(:meth:`Driver.egress_pending`) and delivers each via its scheme's egress driver, then advances the
watermark. Reconciliation (not a fire-and-forget queue) makes it restart-safe — the watermark is the
durable cursor. A run completion wakes it for promptness; a periodic tick is the self-healing fallback.

An egress failure parks the Spout (its own fault/retry state) and never touches the Pond Run — the data
is published and correct locally; egress is downstream of that boundary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

_RECONCILE_INTERVAL = 5.0  # self-healing tick (also catches anything a missed wake left pending)
_PER_SPOUT_TIMEOUT = 60.0  # ceiling on one Spout's delivery, so a slow destination can't starve others


def _egress_spout(root: Path, job: dict) -> None:
    """Deliver one Spout (blocking — runs in the thread pool). Reads the Pond's published tables via the
    data plane and writes each to the destination. Raises on any failure (the caller records it).

    A **transactional, delta-capable** destination (Postgres) syncs *incrementally*: read the changelog
    delta over ``(destination watermark, f]`` and ``apply_delta`` (upserts + deletes), falling back to a
    full reload on a full read (bootstrap / coverage-miss / a changed overwrite source). Others snapshot."""
    import duckdb

    from ..dataplane import get_data_plane
    from ..egress.base import get_egress
    from ..trickle.context import NEVER
    from ..trickle_io import load_sidecar, read_delta
    from .registry import pond_data_dir

    driver = get_egress(job["destination"])  # resolves the scheme's driver (+ validates the URI)
    caps = driver.capabilities()
    data_dir = pond_data_dir(Path(root), job["pond_name"], job["major"])
    dp = get_data_plane()
    f = job["f"]

    con = duckdb.connect()  # in-memory: reads the exported snapshot, never the live registry
    try:
        con.execute("SET TimeZone='UTC'")
        dp.prepare(con)
        sidecar = load_sidecar(data_dir)
        tables = [job["table"]] if job["table"] else dp.list_tables(data_dir)
        for table in tables:
            pk = sidecar.get(table, {}).get("pk") or None
            if caps.supports_delta and caps.transactional:
                if not pk:  # the transactional-PK requirement, enforced at egress for a not-yet-checked source
                    raise ValueError(
                        f"egress to a transactional destination needs a primary key — table {table!r} is "
                        "not a merge Trickle with a declared pk (put a .merge(pk=…) before this Spout)"
                    )
                previous_f = driver.watermark(con, table) or NEVER  # the in-destination cursor (exactly-once)
                delta = read_delta(con, data_dir, table, previous_f, f, dp=dp)
                if delta.is_full:  # bootstrap / coverage-miss / changed overwrite source → reload
                    driver.write_full(con, con.sql(dp.read_select(data_dir, table)), table=table, pk=pk, f=f)
                else:
                    driver.apply_delta(con, delta, table=table, pk=pk, f=f)
            else:
                driver.write_full(con, con.sql(dp.read_select(data_dir, table)), table=table, pk=pk, f=f)
    finally:
        con.close()


async def _drain(driver, root: Path) -> None:
    for job in driver.egress_pending():
        driver.mark_egress_running(job["pond_id"], job["spout"], True)  # "delivering" + no re-dispatch
        try:
            await asyncio.wait_for(run_in_threadpool(_egress_spout, root, job), timeout=_PER_SPOUT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — any delivery error parks the Spout, never the Pond
            driver.record_egress_failure(job["pond_id"], job["spout"], f"{type(exc).__name__}: {exc}")
        else:
            driver.record_egress_success(job["pond_id"], job["spout"], job["f"])
        finally:
            driver.mark_egress_running(job["pond_id"], job["spout"], False)


async def run_egress_worker(driver, root, wake: asyncio.Event) -> None:
    """Drain pending Spouts on each wake (a Pond published / a resync) or on the reconcile tick.
    Cancelled on shutdown."""
    root = Path(root)
    while True:
        try:
            await asyncio.wait_for(wake.wait(), timeout=_RECONCILE_INTERVAL)
        except asyncio.TimeoutError:
            pass  # periodic reconcile
        wake.clear()
        try:
            await _drain(driver, root)
        except Exception as exc:  # keep the loop alive
            print(f"[catchment] egress worker error: {exc}", flush=True)
