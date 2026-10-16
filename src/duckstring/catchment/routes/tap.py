"""Tap management (plans/taps.md): add, list, rotate and remove a Pond line's Taps. All full access, since a
Tap is a credential. The query routes that accept a Tap's key live with the rest of querying (``data.py``,
``serving.py``); :func:`tap_gate` is the step they share: run the Tap's policy, then wait if it says so."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth
from .orchestrate import _driver, _resolve

router = APIRouter()

_WAIT_TICK = 0.1  # how often a waiting Tap query re-checks the Pond's freshness


class _TapBody(BaseModel):
    name: str
    fresh_within_ms: int = 0
    wait_ms: Optional[int] = None
    tables: Optional[list[str]] = None


@router.post("/ponds/{name}/taps", dependencies=[auth.full])
def add_tap(name: str, body: _TapBody, request: Request, major: int | None = None, version: str | None = None):
    """Add a Tap. The response carries its key, once: only a hash of it is kept."""
    key = _resolve(request, name, major, version)
    try:
        tap, secret = _driver(request).add_tap(
            key, body.name, fresh_within=timedelta(milliseconds=body.fresh_within_ms),
            wait=timedelta(milliseconds=body.wait_ms) if body.wait_ms is not None else None, tables=body.tables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out = {"pond": key, "tap": tap, "key": secret}
    driver = _driver(request)
    if not driver.tap_exposes_anything(auth.TapScope(key, tap["name"])):
        # A Tap reads exposed tables only, and an app pointed at one that exposes nothing would see nothing.
        out["warning"] = (f"none of the tables this Tap covers are exposed yet, so its key can't read anything: "
                          f"expose them in pond.toml [serve] tables, or with `duckstring serve expose "
                          f"{driver.meta[key]['name']} TABLE --on`")
    return out


@router.get("/ponds/{name}/taps", dependencies=[auth.full])
def list_taps(name: str, request: Request, major: int | None = None, version: str | None = None):
    return {"taps": _driver(request).list_taps(_resolve(request, name, major, version))}


@router.post("/ponds/{name}/taps/{tap_name}/rotate", dependencies=[auth.full])
def rotate_tap(name: str, tap_name: str, request: Request, major: int | None = None, version: str | None = None):
    """Mint a new key for a Tap (returned once); the old key stops working at once."""
    key = _resolve(request, name, major, version)
    try:
        secret = _driver(request).rotate_tap(key, tap_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    return {"pond": key, "tap": tap_name, "key": secret}


@router.post("/ponds/{name}/taps/{tap_name}/remove", dependencies=[auth.full])
def remove_tap(name: str, tap_name: str, request: Request, major: int | None = None, version: str | None = None):
    key = _resolve(request, name, major, version)
    if not _driver(request).remove_tap(key, tap_name):
        raise HTTPException(status_code=404, detail=f"No Tap '{tap_name}' on '{key}'")
    return {"pond": key, "removed": tap_name}


async def tap_gate(request: Request, tap: auth.TapScope) -> None:
    """A query arrived with a Tap's key: apply the Tap's policy (which may send its Pond a Refresh), then, if
    the Tap waits, hold the request until the data is fresh enough or the Tap's timeout passes."""
    driver = _driver(request)
    try:
        target = driver.tap_query(tap)
        if target is None:
            return
        deadline = asyncio.get_running_loop().time() + driver.tap_wait_timeout(tap)
    except KeyError as exc:  # removed or rotated since the key was checked
        raise HTTPException(status_code=401, detail="Invalid or missing API key") from exc
    while not driver.tap_ready(tap, target) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_WAIT_TICK)
