from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


class _PulseBody(BaseModel):
    version: Optional[int] = None


class _TideBody(BaseModel):
    cron: str
    local: bool = False


@router.get("/status")
def status(request: Request, all: str = "false"):
    db = request.app.state.db
    if all.lower() == "true":
        rows = db.execute(
            """SELECT p.name, pv.version, p.kind
               FROM pond_version pv JOIN pond p ON p.id = pv.pond_id
               ORDER BY p.name, pv.major"""
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT p.name, pv.version, p.kind
               FROM pond_version pv JOIN pond p ON p.id = pv.pond_id
               WHERE pv.is_active = 1
               ORDER BY p.name, pv.major"""
        ).fetchall()
    return {"ponds": [{"name": r[0], "version": r[1], "kind": r[2]} for r in rows]}


@router.post("/outlets/{name}/pulse")
def pulse(name: str, body: _PulseBody = _PulseBody(), request: Request = None):
    db = request.app.state.db
    major = body.version if body.version is not None else 1

    row = db.execute("""
        SELECT pv.id FROM pond_version pv JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = ? AND p.kind = 'outlet' AND pv.major = ? AND pv.is_active = 1
    """, (name, major)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Outlet '{name}' not found")

    pv_id = row[0]
    db.execute(
        "INSERT INTO demand (pond_version_id, sink_id) "
        "SELECT ?, NULL WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ?)",
        (pv_id, pv_id),
    )
    db.commit()

    from ..orchestrator import notify
    notify(request.app)
    return {"ok": True}


@router.post("/outlets/{name}/wave")
def wave(name: str):
    return {"ok": True}


@router.post("/outlets/{name}/tide")
def tide(name: str, body: _TideBody):
    return {"ok": True}
