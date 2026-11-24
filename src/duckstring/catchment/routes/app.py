"""An App's instances, logs and restart (plans/apps.md). Instances are read level, like run history; logs are full,
since an App's output can carry anything its code prints, and so is a restart, like the other Control verbs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ...keys import pond_key
from .. import auth
from ..app_supervisor import instance_rows, version_ids_of
from .orchestrate import _driver, _resolve

router = APIRouter()

_MAX_TAIL_BYTES = 4 * 1024 * 1024


def _app_line(request: Request, name: str, major: int | None, version: str | None) -> tuple[str, int]:
    key = _resolve(request, name, major, version)
    meta = _driver(request).meta.get(key, {})
    if not meta.get("is_app"):
        raise HTTPException(status_code=422, detail=f"'{name}' isn't an App")
    return meta["name"], meta["major"]


@router.get("/ponds/{name}/app/instances", dependencies=[auth.read])
def app_instances(name: str, request: Request, major: int | None = None, version: str | None = None,
                  limit: int = 20):
    """The App line's recent instances, newest first."""
    pond, mj = _app_line(request, name, major, version)
    db = _driver(request).db
    rows = instance_rows(db, version_ids_of(db, pond, mj), limit=max(1, min(limit, 1000)))
    for r in rows:
        r.pop("log_path", None)
    return {"pond": f"{pond}@{mj}", "instances": rows}


@router.get("/ponds/{name}/app/logs", dependencies=[auth.full])
def app_logs(name: str, request: Request, major: int | None = None, version: str | None = None,
             instance: int | None = None, offset: int | None = None, tail: int = 65536):
    """An instance's log (the newest instance by default). With ``offset``, the bytes from there on (to follow
    a log); otherwise the last ``tail`` bytes. The ``x-log-offset`` header is where the next read starts and
    ``x-log-instance`` the instance read."""
    pond, mj = _app_line(request, name, major, version)
    db = _driver(request).db
    rows = instance_rows(db, version_ids_of(db, pond, mj), limit=1000)
    if instance is not None:
        rows = [r for r in rows if r["id"] == instance]
    if not rows:
        raise HTTPException(status_code=404, detail=f"no instance of '{pond}@{mj}'" if instance is None
                            else f"no instance {instance} of '{pond}@{mj}'")
    row = rows[0]
    path = Path(request.app.state.root) / row["log_path"]
    size = path.stat().st_size if path.exists() else 0
    start = max(0, size - min(max(tail, 0), _MAX_TAIL_BYTES)) if offset is None else min(max(offset, 0), size)
    data = b""
    if size > start:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(min(size - start, _MAX_TAIL_BYTES))
    return PlainTextResponse(data.decode("utf-8", errors="replace"),
                             headers={"x-log-offset": str(start + len(data)), "x-log-instance": str(row["id"]),
                                      "x-log-running": "1" if row["stopped_at"] is None else "0"})


@router.post("/ponds/{name}/app/restart", dependencies=[auth.full])
async def app_restart(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Stop the App's instance and start a fresh one, clearing a failure or a Kill first. Returns at once; the
    start shows on ``/api/status``."""
    pond, mj = _app_line(request, name, major, version)
    supervisor = getattr(request.app.state, "app_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="this Catchment isn't running Apps")
    key = pond_key(pond, mj)
    await asyncio.to_thread(_driver(request).prepare_app_restart, key)
    request.app.state.app_restarts = getattr(request.app.state, "app_restarts", set())
    task = asyncio.create_task(supervisor.restart(key))
    request.app.state.app_restarts.add(task)  # held until done, so the task isn't collected mid-start
    task.add_done_callback(request.app.state.app_restarts.discard)
    return {"ok": True}


# ─── scaling (plans/app-scaling.md) ──────────────────────────────────────────────


def _supervisor(request: Request):
    supervisor = getattr(request.app.state, "app_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="this Catchment isn't running Apps")
    return supervisor


@router.get("/ponds/{name}/app/scale", dependencies=[auth.read])
def app_scale(name: str, request: Request, major: int | None = None, version: str | None = None):
    """The App line's scaling: its effective settings and where each comes from, the running instances with what
    the scaler sees of them, recent decisions, and the pressure it is held under at its maximum."""
    pond, mj = _app_line(request, name, major, version)
    return {"pond": f"{pond}@{mj}", **_supervisor(request).scale_status(pond_key(pond, mj))}


class _ScaleBody(BaseModel):
    min_instances: int | None = None
    max_instances: int | None = None
    concurrency: int | None = None
    connections: int | None = None
    requests: float | None = None
    cpu: float | None = None
    memory: float | None = None
    load: float | None = None
    to: int | None = None  # set the count now
    hold_s: float | None = None  # with `to`: keep it exactly for this long
    release: bool = False  # drop a pinned count
    clear: bool = False  # drop every override


@router.post("/ponds/{name}/app/scale", dependencies=[auth.full])
async def set_app_scale(name: str, body: _ScaleBody, request: Request, major: int | None = None,
                        version: str | None = None):
    """Override the App line's scaling settings (kept across redeploys), set its instance count now (``to``), or
    clear the overrides."""
    from datetime import datetime, timedelta, timezone

    pond, mj = _app_line(request, name, major, version)
    key = pond_key(pond, mj)
    driver, supervisor = _driver(request), _supervisor(request)
    fields = body.model_dump(exclude={"to", "hold_s", "release", "clear"})
    for k in ("requests", "cpu", "memory", "load"):
        if fields[k] is not None and not 0 < fields[k] <= 1:
            raise HTTPException(status_code=422, detail=f"{k}: expected a share above 0, at most 1 (0.7 is 70%)")
    for k in ("min_instances", "max_instances", "concurrency", "connections"):
        if fields[k] is not None and fields[k] < (1 if k in ("max_instances", "concurrency") else 0):
            raise HTTPException(status_code=422, detail=f"{k}: too low")
    try:
        if body.clear or any(v is not None for v in fields.values()):
            await asyncio.to_thread(driver.set_app_scale, key, body.clear, **fields)
        if body.release:
            await asyncio.to_thread(driver.set_app_hold, key, None)
        if body.to is not None:
            sc = driver.app_view(key)["scaling"]
            if not 0 <= body.to <= sc["instances"]:
                raise ValueError(f"to: expected 0 to the maximum ({sc['instances']})")
            until = (datetime.now(timezone.utc) + timedelta(seconds=body.hold_s)).isoformat() if body.hold_s else None
            await asyncio.to_thread(driver.set_app_hold, key, body.to, until)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    supervisor._kick(key)
    return {"pond": f"{pond}@{mj}", **supervisor.scale_status(key)}


@router.post("/ponds/{name}/app/instances/{instance}/drain", dependencies=[auth.full])
def drain_app_instance(name: str, instance: int, request: Request, major: int | None = None,
                       version: str | None = None):
    """Send no new sessions to one instance; it stops once its work is done."""
    pond, mj = _app_line(request, name, major, version)
    if not _supervisor(request).drain(pond_key(pond, mj), instance):
        raise HTTPException(status_code=404, detail=f"no running instance {instance} of '{pond}@{mj}'")
    return {"draining": instance}


# ─── Records and Media across majors (app_lifecycle.py) ───────────────────────────


@router.post("/ponds/{name}/app/migrate", dependencies=[auth.full])
async def app_migrate(name: str, request: Request, major: int | None = None, version: str | None = None):
    """Migrate a trial line again from the live one: its Records and Media copied afresh (what was written there is
    discarded) and its ``[app] migrate`` run over them."""
    from ..app_lifecycle import LifecycleError

    app_name, app_major = _app_line(request, name, major, version)
    driver = _driver(request)
    try:
        await asyncio.to_thread(driver.lifecycle.rerun_trial, request.app.state.app_supervisor, app_name, app_major)
    except LifecycleError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"migrated": f"{app_name}@{app_major}", "from": driver.lifecycle.live_major(app_name)}


class _PurgeBody(BaseModel):
    confirm: str | None = None  # the Catchment's name


@router.post("/app-data/{name}/{major}/purge", dependencies=[auth.full])
async def app_purge(name: str, major: int, body: _PurgeBody, request: Request):
    """Delete an App line's Records and Media for good (a retired line's, a trial's, or a removed App's). Refused
    for a deployed live line. Needs the Catchment's name as confirmation."""
    from ..app_lifecycle import LifecycleError
    from .catchment import _catchment_name

    driver = _driver(request)
    expected = _catchment_name(driver.db)
    if (body.confirm or "") != expected:
        raise HTTPException(status_code=422, detail=f"purging deletes {name}@{major}'s Records and Media for "
                                                    f"good; confirm with the Catchment's name: {expected!r}")
    try:
        await asyncio.to_thread(driver.lifecycle.purge, request.app.state.app_supervisor, name, major)
    except LifecycleError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"purged": f"{name}@{major}"}
