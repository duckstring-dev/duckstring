"""Trigger + status endpoints, backed by the freshness :class:`~duckstring.catchment.driver.Driver`.

Tap/Pulse are one-shot; Wave/Tide are standing. Tide carries a staleness **bound** (seconds), not a
cron. Status reports freshness/staleness from the engine, not generations.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


def _driver(request: Request):
    return request.app.state.driver


def _require_pond(request: Request, name: str) -> None:
    if name not in _driver(request).state.ponds:
        raise HTTPException(status_code=404, detail=f"Pond '{name}' not found")


@router.get("/status")
def status(request: Request):
    return _driver(request).status()


@router.get("/runs")
def runs(
    request: Request,
    pond: str | None = None,
    lineage: bool = True,
    ripples: bool = False,
    limit: int = 100,
):
    """Recent Pond Run history (newest first). ``pond`` filters to that Pond and, when ``lineage``,
    its upstream sources; ``ripples`` nests each run's Ripple Runs. ``limit`` is clamped to [1, 1000]."""
    if pond is not None:
        _require_pond(request, pond)
    limit = max(1, min(limit, 1000))
    return {"runs": _driver(request).run_history(pond, lineage, ripples, limit)}


@router.post("/ponds/{name}/tap")
def tap(name: str, request: Request):
    _require_pond(request, name)
    _driver(request).tap(name)
    return {"ok": True}


@router.post("/ponds/{name}/pulse")
def pulse(name: str, request: Request):
    _require_pond(request, name)
    _driver(request).pulse(name)
    return {"ok": True}


@router.post("/ponds/{name}/wave")
def wave(name: str, request: Request):
    _require_pond(request, name)
    _driver(request).wave(name)
    return {"ok": True}


class _TideBody(BaseModel):
    bound_seconds: float


@router.post("/ponds/{name}/tide")
def tide(name: str, body: _TideBody, request: Request):
    _require_pond(request, name)
    if body.bound_seconds <= 0:
        raise HTTPException(status_code=422, detail="bound_seconds must be positive")
    _driver(request).tide(name, timedelta(seconds=body.bound_seconds))
    return {"ok": True}


@router.post("/ponds/{name}/start")
def start(name: str, request: Request):
    """Inject demand directly into a Pond — one run against current inputs, no upstream propagation."""
    _require_pond(request, name)
    _driver(request).start(name)
    return {"ok": True}


class _StopBody(BaseModel):
    upstream: bool = False


@router.post("/ponds/{name}/stop")
def stop(name: str, request: Request, body: _StopBody = _StopBody()):
    """Clear a Pond's demand (push+pull) + its Ripples' pull; keep started runs completing.
    ``upstream`` also stops every ancestor."""
    _require_pond(request, name)
    _driver(request).stop(name, upstream=body.upstream)
    return {"ok": True}


@router.post("/ponds/{name}/untrigger")
def untrigger(name: str, request: Request):
    """Remove the standing Wave/Tide trigger from a Pond (existing work drains)."""
    _require_pond(request, name)
    _driver(request).remove_trigger(name)
    return {"ok": True}


# ─── Failure management ──────────────────────────────────────────────────────


@router.post("/ponds/{name}/clear")
def clear(name: str, request: Request):
    """Clear a failed Pond (the operator okay): resets its failure and unblocks downstream. No run."""
    _require_pond(request, name)
    _driver(request).clear(name)
    return {"ok": True}


class _BudgetBody(BaseModel):
    immediate_retries: int = 0
    source_retries: int = 0


@router.post("/ponds/{name}/budget")
def set_budget(name: str, body: _BudgetBody, request: Request):
    """Set the live retry budgets on a Pond (Ripple retries within a Run; Pond Runs retried on change)."""
    _require_pond(request, name)
    if body.immediate_retries < 0 or body.source_retries < 0:
        raise HTTPException(status_code=422, detail="budgets must be non-negative")
    _driver(request).set_retry(name, body.immediate_retries, body.source_retries)
    return {"ok": True}


@router.get("/ponds/{name}/budget")
def get_budget(name: str, request: Request):
    _require_pond(request, name)
    return _driver(request).retry_config(name)


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
def add_window(name: str, body: _WindowBody, request: Request):
    _require_pond(request, name)
    try:
        _driver(request).add_window(
            name, body.name, body.start_anchor, body.duration_seconds,
            body.freq_unit, body.freq_interval, body.valid_days, body.until_time,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/ponds/{name}/windows")
def list_windows(name: str, request: Request):
    _require_pond(request, name)
    return {"windows": _driver(request).list_windows(name)}


@router.post("/ponds/{name}/windows/{window_name}/remove")
def remove_window(name: str, window_name: str, request: Request):
    _require_pond(request, name)
    if not _driver(request).remove_window(name, window_name):
        raise HTTPException(status_code=404, detail=f"No window '{window_name}' on '{name}'")
    return {"ok": True}
