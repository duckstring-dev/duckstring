"""Trigger + status endpoints, backed by the freshness :class:`~duckstring.catchment.driver.Driver`.

Tap/Pulse are one-shot; Wave/Tide are standing. Tide carries a staleness **bound** (seconds), not a
cron. Status reports freshness/staleness from the engine, not generations.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

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


@router.post("/outlets/{name}/tap")
def tap(name: str, request: Request):
    _require_pond(request, name)
    _driver(request).tap(name)
    return {"ok": True}


@router.post("/outlets/{name}/pulse")
def pulse(name: str, request: Request):
    _require_pond(request, name)
    _driver(request).pulse(name)
    return {"ok": True}


@router.post("/outlets/{name}/wave")
def wave(name: str, request: Request):
    _require_pond(request, name)
    _driver(request).wave(name)
    return {"ok": True}


class _TideBody(BaseModel):
    bound_seconds: float


@router.post("/outlets/{name}/tide")
def tide(name: str, body: _TideBody, request: Request):
    _require_pond(request, name)
    if body.bound_seconds <= 0:
        raise HTTPException(status_code=422, detail="bound_seconds must be positive")
    _driver(request).tide(name, timedelta(seconds=body.bound_seconds))
    return {"ok": True}


class _StopBody(BaseModel):
    version: Optional[int] = None


@router.post("/outlets/{name}/stop")
def stop(name: str, request: Request, body: _StopBody = _StopBody()):
    _require_pond(request, name)
    _driver(request).stop(name)
    return {"ok": True}
