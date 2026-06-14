"""Consumer-side duct management: register conduits into upstream Catchments and choose which of
their Ponds to draw. Each drawn Pond becomes a local Pond Draw (real identity rows, ``is_draw=1``).

A duct carries the upstream's URL + auth headers (forwarded from the CLI's registration on
``duct create``). The poller (see ``app.py``) uses them to dial the upstream; the auth is never
returned to a client (``list_ducts`` redacts it).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


def _driver(request: Request):
    return request.app.state.driver


class _CreateDuctBody(BaseModel):
    origin: str
    remote_url: str
    auth_headers: dict[str, str] | None = None


@router.post("/duct")
def create_duct(body: _CreateDuctBody, request: Request):
    """Register (or update) a conduit from an upstream Catchment."""
    _driver(request).create_duct(body.origin, body.remote_url.rstrip("/"), body.auth_headers)
    return {"ok": True}


@router.get("/duct")
def list_ducts(request: Request):
    return {"ducts": _driver(request).list_ducts()}


@router.delete("/duct/{origin}")
def destroy_duct(origin: str, request: Request):
    if not _driver(request).destroy_duct(origin):
        raise HTTPException(status_code=404, detail=f"No duct from '{origin}'")
    return {"ok": True}


class _AddPondBody(BaseModel):
    pond: str
    major: int = 1
    incremental: bool = False


@router.post("/duct/{origin}/ponds")
def add_pond(origin: str, body: _AddPondBody, request: Request):
    """Draw one upstream Pond over the duct (materialises a Pond Draw)."""
    try:
        _driver(request).add_duct_pond(origin, body.pond, body.major, body.incremental)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/duct/{origin}/ponds/{pond}")
def remove_pond(origin: str, pond: str, request: Request, major: int = 1):
    if not _driver(request).remove_duct_pond(origin, pond, major):
        raise HTTPException(status_code=404, detail=f"'{pond}@{major}' is not drawn from '{origin}'")
    return {"ok": True}


@router.post("/duct/{origin}/sync")
async def sync_duct(origin: str, request: Request):
    """Reflect the upstream's current Ponds into this duct — draw every Pond it exposes. Additive:
    Ponds removed upstream are left as Draws until removed explicitly."""
    driver = _driver(request)
    target = next((d for d in driver.duct_targets() if d["origin"] == origin), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No duct from '{origin}'")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = await client.get(f"{target['remote_url']}/api/status", headers=target["auth"])
            resp.raise_for_status()
            ponds = resp.json().get("ponds", [])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach upstream '{origin}': {exc}") from exc

    added = []
    for p in ponds:
        try:
            driver.add_duct_pond(origin, p["name"], p["major"])
            added.append(f"{p['name']}@{p['major']}")
        except ValueError:
            continue  # a local Pond of that name@major already exists — skip it
    return {"ok": True, "added": added}
