"""The alert worker — delivers queued notifications to their channels. See plans/alerts.md.

One async task in the Catchment process (outbound I/O, the exact shape of the egress worker): each pass it
drains the pending ``alert_delivery`` rows the engine enqueued (:meth:`Driver.take_alert_deliveries`),
resolves each channel's notifier by scheme, ``send``s it in a threadpool with a per-send timeout, and marks
the row sent (:meth:`Driver.mark_delivery_sent`) or bumps attempts / parks it failed at the cap
(:meth:`Driver.mark_delivery_failed`).

**A send failure never propagates into the engine** — it is recorded on the delivery row (auditable via
``alert log``) and retried on the next tick until it succeeds or hits ``MAX_ATTEMPTS``. A permanently-broken
channel therefore stops retrying but leaves a visible failed row, never a Pond failure.
"""

from __future__ import annotations

import asyncio

from fastapi.concurrency import run_in_threadpool

_RECONCILE_INTERVAL = 5.0  # self-healing tick — retries pending deliveries a previous send left behind
_PER_SEND_TIMEOUT = 30.0   # ceiling on one delivery, so a slow channel can't starve the others
MAX_ATTEMPTS = 6           # after this many failed sends, park the delivery 'failed' (stop retrying)


def _deliver(destination: str, payload: dict) -> None:
    """Send one notification (blocking — runs in the thread pool). Raises on any failure."""
    from ..alerts import AlertEvent, get_notifier

    notifier = get_notifier(destination)  # resolves the scheme's driver (+ validates the URI)
    notifier.send(AlertEvent.from_payload(payload))


async def _drain(driver) -> None:
    for row in driver.take_alert_deliveries():
        try:
            await asyncio.wait_for(
                run_in_threadpool(_deliver, row["destination"], row["payload"]),
                timeout=_PER_SEND_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — any delivery error is recorded, never raised into the engine
            driver.mark_delivery_failed(row["id"], f"{type(exc).__name__}: {exc}", MAX_ATTEMPTS)
        else:
            driver.mark_delivery_sent(row["id"])


async def run_alert_worker(driver, wake: asyncio.Event) -> None:
    """Drain queued alert deliveries on each wake (a delivery was enqueued) or the reconcile tick.
    Cancelled on shutdown."""
    while True:
        try:
            await asyncio.wait_for(wake.wait(), timeout=_RECONCILE_INTERVAL)
        except asyncio.TimeoutError:
            pass  # periodic reconcile — retry anything still pending
        wake.clear()
        try:
            await _drain(driver)
        except Exception as exc:  # keep the loop alive
            print(f"[catchment] alert worker error: {exc}", flush=True)
