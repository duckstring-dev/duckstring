"""Puddle Sources on a Catchment (catchment/puddles.py): a line's Puddle Sources, and Refreshes through them.

``GET  /api/ponds/{name}/puddles``          the line's Puddle Sources (read)
``POST /api/ponds/{name}/puddles``          replace them from a parsed ``puddles.toml`` (full; ``pond puddle push``)
``POST /api/ponds/{name}/puddles/refresh``  move the Puddles to a target and refresh at it (demand)
``POST /api/ponds/{name}/puddles/hydrate``  take a new baseline from production and run from it (full: it stores
                                            production's credentials and resets the Ponds it reaches)
``POST /api/ponds/{name}/puddles/rewind``   back to the baseline and run from it (full: it resets them too)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import auth
from .orchestrate import _driver, _resolve

router = APIRouter()


def _point(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        f = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label}: not an ISO time ({value!r})") from exc
    return f if f.tzinfo is not None else f.replace(tzinfo=timezone.utc)


async def _call(fn, *args, **kwargs):
    from ...remote_source import RemoteQueryError

    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteQueryError as exc:
        raise HTTPException(status_code=502, detail=_origin_error(exc)) from exc


def _origin_error(exc) -> str:
    if exc.status in (401, 403):
        return ("the Catchment the Puddles read refused the credentials stored here: refresh with --hydrate "
                "--from CATCHMENT to store new ones")
    return f"the Catchment the Puddles read answered {exc.status}: {exc.detail}"


@router.get("/ponds/{name}/puddles", dependencies=[auth.read])
def list_puddles(name: str, request: Request, major: int | None = None, version: str | None = None):
    key = _resolve(request, name, major, version)
    sources = _driver(request).meta[key].get("puddle_sources") or {}
    return {"puddle_sources": [{"source": s, "set": v} for s, v in sorted(sources.items())]}


class _PushBody(BaseModel):
    puddles: dict | None = None  # the parsed puddles.toml; this Catchment takes its own section


@router.post("/ponds/{name}/puddles", dependencies=[auth.full])
async def push_puddles(name: str, body: _PushBody, request: Request, major: int | None = None,
                       version: str | None = None):
    driver = _driver(request)
    key = _resolve(request, name, major, version)
    try:
        section = driver.puddle_section(body.puddles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"puddle_sources": await _call(driver.set_puddle_sources, key, section)}


class _TargetBody(BaseModel):
    target: str | None = None  # next-change | next-run | most-recent
    to: str | None = None      # ISO freshness


@router.post("/ponds/{name}/puddles/refresh", dependencies=[auth.demand])
async def refresh_puddles(name: str, body: _TargetBody, request: Request, major: int | None = None,
                          version: str | None = None):
    if (body.target is None) == (body.to is None):
        raise HTTPException(status_code=422, detail="give one of target or to")
    key = _resolve(request, name, major, version)
    asked = await _call(_driver(request).refresh_puddles, key, target=body.target, to=_point(body.to, "to"))
    return {"ok": True, "requested": asked.isoformat()}


class _Origin(BaseModel):
    name: str | None = None  # the CLI's name for it, shown only
    url: str
    headers: dict[str, str] = {}


class _HydrateBody(BaseModel):
    origin: _Origin
    live: bool = False
    as_at: str | None = None


@router.post("/ponds/{name}/puddles/hydrate", dependencies=[auth.full])
async def hydrate_puddles(name: str, body: _HydrateBody, request: Request, major: int | None = None,
                          version: str | None = None):
    key = _resolve(request, name, major, version)
    asked = await _call(_driver(request).refresh_puddles, key, hydrate=True, live=body.live,
                        as_at=_point(body.as_at, "as_at"), origin=body.origin.model_dump())
    return {"ok": True, "requested": asked.isoformat()}


@router.post("/ponds/{name}/puddles/rewind", dependencies=[auth.full])
async def rewind_puddles(name: str, request: Request, major: int | None = None, version: str | None = None):
    key = _resolve(request, name, major, version)
    asked = await _call(_driver(request).refresh_puddles, key, rewind=True)
    return {"ok": True, "requested": asked.isoformat()}
