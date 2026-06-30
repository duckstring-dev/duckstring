"""A **writer lease** on the data root — defence against two Catchments operating on one lake.

The Iceberg catalog pointer (``catalog.json``) is the one shared-mutable object the data plane has; a
plain object PUT is safe **only** under single-writer-per-line, which the runtime already enforces (one
Duck per ``name@major``, "run exactly ONE app process"). This lease guards the case that invariant can't:
a *second Catchment* — a stray autoscaled app process, a rolling-deploy overlap, or two deployments
misconfigured to share a data root — writing the same catalogs concurrently, which would race the pointer
and (with the GC) dangle it.

It is a **lease, not a distributed lock**: a small ``_duckstring_owner.json`` object at the data-root top
level, written with a plain PUT (portable across S3/GCS/ABFS/local — no backend-specific conditional-PUT)
and renewed on an interval. So it *detects and narrows* the dangerous window rather than making it
impossible — for an airtight mutex you'd need a CAS/conditional PUT or a real lock service. A read-back
verification after acquisition catches the common simultaneous-boot race.

Semantics (owner = the Catchment's stable ``id`` from ``catchment_meta``):

- **No lease / expired lease** (no renewal within the TTL → the owner is presumed dead) → acquire it.
- **Our own id** (a restart / redeploy of the *same* Catchment) → reclaim instantly, preserving the
  original ``acquired_at``. So a normal restart never blocks.
- **A different, live id** → refuse to start (raise :class:`LeaseConflict`), unless
  ``DUCKSTRING_FORCE_TAKEOVER=1`` is set (steal it, loudly).

Engaged **only when an external ``DUCKSTRING_DATA_ROOT`` is set** — a local data root under the state
root has no sharing risk, so the default single-disk behaviour is untouched. Assumes **one Catchment per
data root**: to run several against one bucket, give each a distinct prefix.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone

LEASE_NAME = "_duckstring_owner.json"
_DEFAULT_TTL = 120.0  # seconds without a renewal before the owner is presumed dead and the lease takeable


class LeaseConflict(RuntimeError):
    """The data root is held by a different, live Catchment — refusing to start (set
    ``DUCKSTRING_FORCE_TAKEOVER=1`` to steal it)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(storage) -> dict | None:
    raw = storage.read_text(LEASE_NAME)
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except ValueError:
        return None  # malformed → treat as no lease (takeable)


def _expired(rec: dict, now: datetime) -> bool:
    try:
        renewed = datetime.fromisoformat(rec["renewed_at"])
    except (KeyError, ValueError):
        return True
    ttl = float(rec.get("ttl_seconds", _DEFAULT_TTL))
    return (now - renewed).total_seconds() > ttl


def _record(owner_id: str, acquired_at: datetime, now: datetime, ttl: float) -> dict:
    return {
        "catchment_id": owner_id,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "acquired_at": acquired_at.isoformat(),
        "renewed_at": now.isoformat(),
        "ttl_seconds": ttl,
    }


def force_takeover() -> bool:
    return os.environ.get("DUCKSTRING_FORCE_TAKEOVER", "").lower() in ("1", "true", "yes")


def acquire_lease(storage, owner_id: str, *, ttl: float = _DEFAULT_TTL, force: bool | None = None) -> dict:
    """Take (or renew) the data-root lease for ``owner_id``. Raises :class:`LeaseConflict` if a *different*,
    live Catchment holds it (unless ``force``). Returns the written record."""
    if force is None:
        force = force_takeover()
    now = _now()
    existing = _read(storage)
    acquired_at = now
    if existing is not None:
        same = existing.get("catchment_id") == owner_id
        if same:
            try:
                acquired_at = datetime.fromisoformat(existing["acquired_at"])  # preserve original
            except (KeyError, ValueError):
                acquired_at = now
        elif not _expired(existing, now) and not force:
            age = (now - datetime.fromisoformat(existing["renewed_at"])).total_seconds()
            raise LeaseConflict(
                f"data root is held by Catchment {existing.get('catchment_id')!r} "
                f"(host {existing.get('host')}, pid {existing.get('pid')}, renewed {age:.0f}s ago). "
                "Run exactly one Catchment per data root, or set DUCKSTRING_FORCE_TAKEOVER=1 to steal it."
            )
    rec = _record(owner_id, acquired_at, now, ttl)
    storage.write_text(json.dumps(rec), LEASE_NAME)
    # Read-back verification: catches the simultaneous-boot race two plain PUTs can otherwise lose silently.
    back = _read(storage)
    if not force and (back is None or back.get("catchment_id") != owner_id):
        raise LeaseConflict(
            "lost a concurrent data-root acquisition race to "
            f"{(back or {}).get('catchment_id')!r} — another Catchment is starting on this data root"
        )
    return rec


def renew_lease(storage, owner_id: str, *, ttl: float = _DEFAULT_TTL) -> bool:
    """Refresh our lease's ``renewed_at``. Returns ``False`` (without writing) if the lease is no longer
    ours — someone force-stole it; the caller should treat that as a fatal divergence."""
    existing = _read(storage)
    if existing is not None and existing.get("catchment_id") != owner_id:
        return False
    acquired_at = _now()
    if existing is not None:
        try:
            acquired_at = datetime.fromisoformat(existing["acquired_at"])
        except (KeyError, ValueError):
            pass
    storage.write_text(json.dumps(_record(owner_id, acquired_at, _now(), ttl)), LEASE_NAME)
    return True


def release_lease(storage, owner_id: str) -> None:
    """Drop the lease on graceful shutdown, but only if it is still ours (never steal a successor's)."""
    existing = _read(storage)
    if existing is not None and existing.get("catchment_id") == owner_id:
        storage.remove(LEASE_NAME)


async def run_lease_renewer(storage, owner_id: str, *, ttl: float = _DEFAULT_TTL) -> None:
    """Renew the data-root lease every ``ttl/3`` so a live Catchment's ownership never lapses. If the lease
    is found stolen, log loudly and keep trying (a hard stop mid-run would be worse than a warning)."""
    import asyncio

    from fastapi.concurrency import run_in_threadpool

    while True:
        await asyncio.sleep(max(10.0, ttl / 3))
        try:
            ok = await run_in_threadpool(renew_lease, storage, owner_id, ttl=ttl)
            if not ok:
                print(
                    f"[catchment] WARNING: data-root lease was taken over by another Catchment — "
                    f"this instance ({owner_id}) no longer owns the data plane; stop one of them",
                    flush=True,
                )
        except Exception as exc:  # pragma: no cover - renewal must not crash the loop
            print(f"[catchment] data-root lease renewal failed: {exc}", flush=True)
