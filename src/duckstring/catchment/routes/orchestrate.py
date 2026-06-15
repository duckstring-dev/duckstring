"""Trigger + status endpoints, backed by the freshness :class:`~duckstring.catchment.driver.Driver`.

Tap/Pulse are one-shot; Wave/Tide are standing. Tide carries a staleness **bound** (seconds), not a
cron. Status reports freshness/staleness from the engine, not generations.

Every pond-targeting route takes optional ``major`` / ``version`` query params: ``major`` picks the
major line (default: the highest deployed), ``version`` additionally requires that exact version to
be the line's currently selected artifact. The resolved target is the engine key ``name@major``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

_STATUS_WAIT_TICK = 0.05  # how often the status long-poll re-checks the state version
_STATUS_WAIT_TIMEOUT = 25.0  # heartbeat ceiling on a held status request


def _driver(request: Request):
    return request.app.state.driver


def _parse_f(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 demand epoch (a duct forwards the downstream's freshness)."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid freshness {value!r}") from exc


def _resolve(request: Request, name: str, major: int | None, version: str | None) -> str:
    """Resolve a Pond reference to its engine key, mapping resolution errors to HTTP ones."""
    try:
        return _driver(request).resolve(name, major, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/status")
async def status(request: Request, since: Optional[int] = None):
    """Live state. Without ``since``, returns immediately (the CLI / first load). With ``since``, it
    **long-polls**: holds until the engine state moves past that version (or a heartbeat timeout), so
    the UI updates the instant anything changes instead of on a fixed timer. The payload's ``version``
    is the token to pass back as ``since``."""
    driver = _driver(request)
    if since is not None:
        for _ in range(int(_STATUS_WAIT_TIMEOUT / _STATUS_WAIT_TICK)):
            if driver.state_version != since:
                break
            await asyncio.sleep(_STATUS_WAIT_TICK)
    return driver.status()


@router.get("/runs")
def runs(
    request: Request,
    pond: str | None = None,
    major: int | None = None,
    version: str | None = None,
    lineage: bool = True,
    ripples: bool = False,
    limit: int = 100,
):
    """Recent Pond Run history (newest first). ``pond`` filters to that Pond and, when ``lineage``,
    its upstream sources; ``ripples`` nests each run's Ripple Runs. ``limit`` is clamped to [1, 1000]."""
    key = _resolve(request, pond, major, version) if pond is not None else None
    limit = max(1, min(limit, 1000))
    return {"runs": _driver(request).run_history(key, lineage, ripples, limit)}


@router.post("/ponds/{name}/tap")
def tap(
    name: str, request: Request, major: int | None = None, version: str | None = None,
    m: str | None = None,
):
    """``m`` (optional ISO freshness) is the demand epoch to mint — a duct forwards the downstream's."""
    _driver(request).tap(_resolve(request, name, major, version), _parse_f(m))
    return {"ok": True}


@router.post("/ponds/{name}/pulse")
def pulse(
    name: str, request: Request, major: int | None = None, version: str | None = None,
    at: str | None = None,
):
    """``at`` (optional ISO freshness) is the push target — a duct forwards the downstream's target."""
    _driver(request).pulse(_resolve(request, name, major, version), _parse_f(at))
    return {"ok": True}


@router.post("/ponds/{name}/wave")
def wave(name: str, request: Request, major: int | None = None, version: str | None = None):
    _driver(request).wave(_resolve(request, name, major, version))
    return {"ok": True}


class _TideBody(BaseModel):
    bound_seconds: float


@router.post("/ponds/{name}/tide")
def tide(name: str, body: _TideBody, request: Request, major: int | None = None, version: str | None = None):
    key = _resolve(request, name, major, version)
    if body.bound_seconds <= 0:
        raise HTTPException(status_code=422, detail="bound_seconds must be positive")
    _driver(request).tide(key, timedelta(seconds=body.bound_seconds))
    return {"ok": True}


# ─── Control (Wake / Sleep / Force / Kill) ───────────────────────────────────


@router.post("/ponds/{name}/wake")
def wake(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Wake a Pond — a one-shot non-propagating pull: run once on fresh input, no upstream solicit."""
    _driver(request).wake(_resolve(request, name, major, version))
    return {"ok": True}


@router.post("/ponds/{name}/force")
def force(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Force a Pond to recompute now at its current freshness, even with no upstream change."""
    _driver(request).force(_resolve(request, name, major, version))
    return {"ok": True}


@router.post("/ponds/{name}/kill")
def kill(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Kill a Pond — terminate its Duck and park it in a terminal killed state (cancels its Run)."""
    _driver(request).kill(_resolve(request, name, major, version))
    return {"ok": True}


class _SleepBody(BaseModel):
    upstream: bool = False


@router.post("/ponds/{name}/sleep")
def sleep(
    name: str, request: Request, body: _SleepBody = _SleepBody(),
    major: int | None = None, version: str | None = None,
):
    """Sleep a Pond — clear its demand (push+pull) + its Ripples' pull; keep started runs completing.
    ``upstream`` also sleeps every ancestor."""
    _driver(request).sleep(_resolve(request, name, major, version), upstream=body.upstream)
    return {"ok": True}


@router.post("/ponds/{name}/untrigger")
def untrigger(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Remove the standing Wave/Tide trigger from a Pond (existing work drains)."""
    _driver(request).remove_trigger(_resolve(request, name, major, version))
    return {"ok": True}


# ─── Failure management ──────────────────────────────────────────────────────


@router.post("/ponds/{name}/clear")
def clear(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Clear a failed Pond (the operator okay): resets its failure and unblocks downstream. No run."""
    _driver(request).clear(_resolve(request, name, major, version))
    return {"ok": True}


class _BudgetBody(BaseModel):
    immediate_retries: int = 0
    source_retries: int = 0


@router.post("/ponds/{name}/budget")
def set_budget(
    name: str, body: _BudgetBody, request: Request,
    major: int | None = None, version: str | None = None,
):
    """Set the live retry budgets on a Pond (Ripple retries within a Run; Pond Runs retried on change)."""
    key = _resolve(request, name, major, version)
    if body.immediate_retries < 0 or body.source_retries < 0:
        raise HTTPException(status_code=422, detail="budgets must be non-negative")
    _driver(request).set_retry(key, body.immediate_retries, body.source_retries)
    return {"ok": True}


@router.get("/ponds/{name}/budget")
def get_budget(name: str, request: Request, major: int | None = None, version: str | None = None):
    return _driver(request).retry_config(_resolve(request, name, major, version))


# ─── Cross-Catchment exposure (open / tap-on-get) ────────────────────────────


class _OpenBody(BaseModel):
    tap_on_get: bool = False


@router.post("/ponds/{name}/open")
def open_pond(
    name: str, request: Request, body: _OpenBody = _OpenBody(),
    major: int | None = None, version: str | None = None,
):
    """Mark a Pond open — it accepts demand from any source (e.g. a downstream Catchment over a duct).
    With ``tap_on_get`` a read on the query route also fires a Tap (the snapshot is served first)."""
    _driver(request).set_pond_open(_resolve(request, name, major, version), body.tap_on_get)
    return {"ok": True}


@router.post("/ponds/{name}/close")
def close_pond(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Close a Pond — remove its open flag (and tap-on-get)."""
    _driver(request).unset_pond_open(_resolve(request, name, major, version))
    return {"ok": True}


# ─── Windows (batch-availability on Inlets) ──────────────────────────────────────


class _WindowBody(BaseModel):
    name: str
    start_anchor: str
    duration_seconds: int
    freq_unit: str
    freq_interval: int = 1
    valid_days: str | None = None
    until_time: str | None = None


@router.post("/ponds/{name}/windows")
def add_window(
    name: str, body: _WindowBody, request: Request,
    major: int | None = None, version: str | None = None,
):
    key = _resolve(request, name, major, version)
    try:
        _driver(request).add_window(
            key, body.name, body.start_anchor, body.duration_seconds,
            body.freq_unit, body.freq_interval, body.valid_days, body.until_time,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/ponds/{name}/windows")
def list_windows(name: str, request: Request, major: int | None = None, version: str | None = None):
    return {"windows": _driver(request).list_windows(_resolve(request, name, major, version))}


@router.post("/ponds/{name}/windows/{window_name}/remove")
def remove_window(
    name: str, window_name: str, request: Request,
    major: int | None = None, version: str | None = None,
):
    key = _resolve(request, name, major, version)
    if not _driver(request).remove_window(key, window_name):
        raise HTTPException(status_code=404, detail=f"No window '{window_name}' on '{name}'")
    return {"ok": True}
