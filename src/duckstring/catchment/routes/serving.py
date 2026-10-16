"""The serving surface (plans/data-serving.md) — the Catalog: list what's serviceable, run read-only
SQL through the sandboxed serving core, and (full) manage exposure + the served-major pointer.

Read/demand users get the **sandboxed** core (serviceable-only, no external access) and see only exposed
tables; full users get the ops core (all output tables) and the exposure/promote controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import auth
from ..serving import serving_query

router = APIRouter()


def _driver(request: Request):
    return request.app.state.driver


def _is_full(principal: auth.Principal) -> bool:
    return principal.level is not None and principal.level >= auth.Level.FULL


@router.get("/serve", dependencies=[auth.read_or_tap])
def catalog(request: Request, principal: auth.Principal = Depends(auth.get_principal)):
    """The catalog: per pond, its served major + deployed majors + tables. Read users see only exposed
    tables (of every major, for pinning); full users see every output table with its exposure source. A
    Tap's key sees its own Pond line and tables only."""
    from ...keys import pond_key

    driver = _driver(request)
    full = _is_full(principal)
    tap = principal.tap
    with driver.lock:
        if tap is not None:
            meta = driver.meta.get(tap.pond) or {}
            allowed = driver.tap_tables(tap) if meta else set()
            tables = [{**d, "major": meta["major"]} for d in driver.serve_detail(tap.pond)
                      if d.get("table") in allowed] if meta else []
            return {"catchment": (driver.status().get("catchment") or {}).get("name"),
                    "tap": tap.name,
                    "ponds": [{"name": meta.get("name"), "served_major": meta.get("major"),
                               "majors": [meta.get("major")], "tables": tables}] if meta else [],
                    "connect": _connect(request)}
        names = sorted({m["name"] for m in driver.meta.values()
                        if not m.get("is_draw") and not m.get("is_spout")})
        ponds = []
        for name in names:
            majors = driver.deployed_majors(name)
            if not majors:
                continue
            served = driver.served_major(name) or majors[0]
            tables = []
            for major in majors:
                for d in driver.serve_detail(pond_key(name, major)):
                    if full or d["exposed"]:
                        tables.append({**d, "major": major})
            ponds.append({"name": name, "served_major": served, "majors": majors, "tables": tables})
    return {"catchment": (driver.status().get("catchment") or {}).get("name"), "ponds": ponds,
            "connect": _connect(request)}


def _connect(request: Request) -> dict:
    connect = {}
    for wire in getattr(request.app.state, "serve_wires", []):
        connect[getattr(wire, "kind", "?")] = f"{getattr(wire, 'host', '127.0.0.1')}:{wire.port}"
    return connect


class _QueryBody(BaseModel):
    sql: str
    limit: int | None = 1000  # capped page for the JSON surface (the wires stream unbounded)


@router.post("/serve/query", dependencies=[auth.read_or_tap])
async def query(body: _QueryBody, request: Request,
                principal: auth.Principal = Depends(auth.get_principal)):
    """Run read-only SQL against the serving surface. Sandboxed unless the caller is full; a Tap's key runs on
    the Tap's own connection, after its policy (plans/taps.md). Off the event loop — the core
    materialises/queries synchronously."""
    from .tap import tap_gate

    driver = _driver(request)
    sandboxed = not _is_full(principal)
    if principal.tap is not None:
        await tap_gate(request, principal.tap)
    sql = body.sql.strip().rstrip(";")
    if body.limit and body.limit > 0:
        sql = f"SELECT * FROM ({sql}) AS _q LIMIT {int(body.limit)}"
    try:
        res = await run_in_threadpool(serving_query, driver, sql, sandboxed=sandboxed, tap=principal.tap)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"columns": res["columns"], "rows": [list(r) for r in res["rows"]]}


class _PromoteBody(BaseModel):
    major: int


@router.post("/ponds/{name}/serve/promote", dependencies=[auth.full])
def promote(name: str, body: _PromoteBody, request: Request):
    try:
        _driver(request).promote(name, body.major)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, "served_major": body.major}


class _ExposeBody(BaseModel):
    table: str
    exposed: bool | None = None  # True expose / False hide / None clear the override (→ declared default)


@router.post("/ponds/{name}/serve/expose", dependencies=[auth.full])
def expose(name: str, body: _ExposeBody, request: Request,
           major: int | None = None, version: str | None = None):
    from .orchestrate import _resolve

    key = _resolve(request, name, major, version)
    _driver(request).set_exposed(key, body.table, body.exposed)
    return {"ok": True}
