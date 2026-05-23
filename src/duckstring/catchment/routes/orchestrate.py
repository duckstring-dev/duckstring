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
    where = "" if all.lower() == "true" else "WHERE pv.is_active = 1"
    rows = db.execute(f"""
        SELECT
            p.name,
            pv.version,
            p.kind,
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM pond_run pr
                    WHERE pr.pond_version_id = pv.id AND pr.status = 'running'
                ) THEN 'running'
                WHEN EXISTS (
                    SELECT 1 FROM demand d WHERE d.pond_version_id = pv.id
                ) THEN 'queued'
                WHEN (
                    SELECT pr.status FROM pond_run pr
                    WHERE pr.pond_version_id = pv.id AND pr.finished_at IS NOT NULL
                    ORDER BY pr.finished_at DESC LIMIT 1
                ) = 'failed' THEN 'failed'
                ELSE 'idle'
            END AS status,
            COALESCE((
                SELECT MAX(pr.generation) FROM pond_run pr
                JOIN pond_version pv2 ON pv2.id = pr.pond_version_id
                WHERE pv2.pond_id = p.id AND pv2.major = pv.major AND pr.status = 'success'
            ), 0) AS last_gen,
            (
                SELECT pr.finished_at FROM pond_run pr
                WHERE pr.pond_version_id = pv.id AND pr.finished_at IS NOT NULL
                ORDER BY pr.finished_at DESC LIMIT 1
            ) AS last_run_at,
            (
                SELECT pr.status FROM pond_run pr
                WHERE pr.pond_version_id = pv.id AND pr.finished_at IS NOT NULL
                ORDER BY pr.finished_at DESC LIMIT 1
            ) AS last_run_status
        FROM pond_version pv
        JOIN pond p ON p.id = pv.pond_id
        {where}
        ORDER BY p.name, pv.major
    """).fetchall()
    edge_rows = db.execute("""
        SELECT p_src.name, p_sink.name
        FROM pond_to_pond e
        JOIN pond_version pv ON pv.id = e.pond_version_id AND pv.is_active = 1
        JOIN pond p_sink ON p_sink.id = pv.pond_id
        JOIN pond p_src ON p_src.id = e.source_pond_id
    """).fetchall()
    return {
        "ponds": [
            {
                "name": r[0], "version": r[1], "kind": r[2],
                "status": r[3], "gen": r[4],
                "last_run_at": r[5], "last_run_status": r[6],
            }
            for r in rows
        ],
        "edges": [[r[0], r[1]] for r in edge_rows],
    }


@router.post("/outlets/{name}/pulse")
def pulse(name: str, body: _PulseBody = _PulseBody(), request: Request = None):
    db = request.app.state.db
    major = body.version if body.version is not None else 1

    row = db.execute("""
        SELECT pv.id, pv.version FROM pond_version pv JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = ? AND p.kind = 'outlet' AND pv.major = ? AND pv.is_active = 1
    """, (name, major)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Outlet '{name}' not found")

    pv_id, version = row
    db.execute(
        "INSERT INTO demand (pond_version_id, sink_id) "
        "SELECT ?, NULL WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ?)",
        (pv_id, pv_id),
    )
    db.commit()

    from ..orchestrator import notify, _log
    _log("demand", f"{name} v{version}")
    notify(request.app)
    return {"ok": True}


@router.post("/outlets/{name}/wave")
def wave(name: str):
    return {"ok": True}


@router.post("/outlets/{name}/tide")
def tide(name: str, body: _TideBody):
    return {"ok": True}
