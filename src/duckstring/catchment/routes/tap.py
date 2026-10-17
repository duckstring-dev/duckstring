"""Tap management (plans/taps.md): add, list, rotate and remove a Pond line's Taps. All full access, since a
Tap is a credential. The query routes that accept a Tap's key live with the rest of querying (``data.py``,
``serving.py``); :func:`tap_gate` is the step they share: run the Tap's policy."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth
from .orchestrate import _driver, _resolve

router = APIRouter()


class _TapBody(BaseModel):
    name: str
    fresh_within_ms: int = 0


@router.post("/ponds/{name}/taps", dependencies=[auth.full])
def add_tap(name: str, body: _TapBody, request: Request, major: int | None = None, version: str | None = None):
    """Add a Tap. The response carries its key, once: only a hash of it is kept."""
    key = _resolve(request, name, major, version)
    try:
        tap, secret = _driver(request).add_tap(key, body.name, fresh_within=timedelta(milliseconds=body.fresh_within_ms))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"pond": key, "tap": tap, "key": secret}


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


def tap_gate(request: Request, tap: auth.TapScope) -> None:
    """A query arrived with a Tap's key: apply the Tap's policy, which may send its Pond a Refresh. The query is
    answered at once either way."""
    try:
        _driver(request).tap_query(tap)
    except KeyError as exc:  # removed or rotated since the key was checked
        raise HTTPException(status_code=401, detail="Invalid or missing API key") from exc
