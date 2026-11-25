"""Duck protocol endpoints.

``GET  /api/duck/{name}/{major}/jobs``     — the Duck polls for queued commands (``begin_run`` / ``shutdown``).
``POST /api/duck/{name}/{major}/events``   — the Duck reports ``ripple`` / ``run_completed`` events.
``GET  /api/duck/{name}/{major}/artifact`` — the deployed version's source bundle (a tar), for a
remote Duck that can't read the Catchment's disk (prereqs D6). The Catchment stays the artifact
authority — no object-store coupling here.

A Duck serves one major line of a Pond, so all routes address the pond key ``name@major``. All are
Duck-initiated, so the same shape works for a local subprocess or a remote Duck. Events are idempotent on
freshness.

**Job delivery is a poll with a held wake-up.** A poll returns whatever is queued at once. With nothing
queued, a poll asking to ``wait`` is held for up to ``JOB_HOLD_S`` and returns *empty* the moment a job is
queued for its Pond (``{"wake": true}``), or when the hold ends (``{"held": true}``); either way the Duck
polls again at once, and that poll collects the job. A held poll never takes a job itself. ``take_jobs``
drains the queue, and a Duck killed mid-poll leaves its request open (ASGI gives no reliable disconnect
signal for an idle GET), so a held poll that drained would hand the next job to a dead Duck and its
replacement would wait forever: that broke reset and missing-source recovery when it was tried. As a
wake-up, a dead Duck's request takes nothing, and a job reaches a live Duck one round trip after it is
queued, instead of after the Duck's next backed-off poll (up to 2 s, once per stage of a pipeline). The
hold is short so a held request never delays a server's shutdown for long.
"""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from starlette.concurrency import run_in_threadpool

from ...keys import pond_key
from .. import auth

router = APIRouter()


# The longest a job poll with nothing queued is held (see the module docstring).
JOB_HOLD_S = 2.0


def wake_job_polls(app, key: str) -> None:
    """Release every poll held for ``key``'s Duck (called on the event loop when a job is queued)."""
    for event in app.state.job_polls.get(key, ()):
        event.set()


@router.get("/duck/{name}/{major}/jobs", dependencies=[auth.duck])
async def jobs(name: str, major: int, request: Request, version: str | None = None, wait: float = 0.0):
    driver = request.app.state.driver
    key = pond_key(name, major)
    hold = min(max(wait, 0.0), JOB_HOLD_S)
    polls = request.app.state.job_polls  # pond key -> the events of its held polls
    event = asyncio.Event()
    if hold:
        polls.setdefault(key, set()).add(event)  # before taking, so a job queued meanwhile still wakes it
    try:
        queued = await run_in_threadpool(driver.take_jobs, key, version)
        if queued or not hold:
            return {"jobs": queued}
        try:
            await asyncio.wait_for(event.wait(), hold)
            return {"jobs": [], "wake": True}
        except asyncio.TimeoutError:
            return {"jobs": [], "held": True}
    finally:
        waiting = polls.get(key)
        if waiting is not None:
            waiting.discard(event)
            if not waiting:
                polls.pop(key, None)


@router.post("/duck/{name}/{major}/events", dependencies=[auth.duck])
async def events(name: str, major: int, request: Request):
    driver = request.app.state.driver
    payload = await request.json()
    driver.on_event(pond_key(name, major), payload)
    return {"ok": True}


@router.post("/duck/{name}/{major}/puddle/query", dependencies=[auth.duck])
async def puddle_query(name: str, major: int, request: Request):
    """A Duck's query of one of its line's Puddle Sources, forwarded to the Catchment the Puddles read
    (catchment/puddles.py): the Duck never holds that Catchment's credentials. Its answer (Parquet, and any
    warning header) comes back as it is; a refusal keeps its status and detail."""
    from ...remote_source import RemoteQueryError
    from .puddles import _origin_error

    driver = request.app.state.driver
    payload = await request.json()
    try:
        content, warning = await run_in_threadpool(driver.puddle_query, pond_key(name, major), payload)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RemoteQueryError as exc:
        status = 502 if exc.status in (401, 403) else exc.status
        raise HTTPException(status_code=status, detail=_origin_error(exc) if status == 502 else exc.detail) from exc
    headers = {"x-duckstring-warning": warning} if warning else {}
    return Response(content=content, media_type="application/octet-stream", headers=headers)


@router.post("/duck/{name}/{major}/puddle/get", dependencies=[auth.duck])
async def puddle_get(name: str, major: int, request: Request):
    """A Duck's read of a Puddle Source's table list, forwarded as :func:`puddle_query` is."""
    from ...remote_source import RemoteQueryError
    from .puddles import _origin_error

    driver = request.app.state.driver
    body = await request.json()
    try:
        return await run_in_threadpool(driver.puddle_get, pond_key(name, major), body.get("path", ""),
                                       body.get("params"))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RemoteQueryError as exc:
        status = 502 if exc.status in (401, 403) else exc.status
        raise HTTPException(status_code=status, detail=_origin_error(exc) if status == 502 else exc.detail) from exc


# ─── Apps' Records and Media for a Duck on another machine (plans/records-and-media.md) ───────────────


@router.post("/duck/{name}/{major}/records/read", dependencies=[auth.duck])
async def records_read(name: str, major: int, request: Request):
    """One Records table as Parquet, for a Duck that can't reach this Catchment's PostgreSQL (it listens on this
    machine). Read as the reader role: Ponds read every line's Records."""
    import tempfile

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from ..records import RecordsError

    body = await request.json()
    records = request.app.state.driver.records
    fd, path = tempfile.mkstemp(suffix=".parquet", prefix="duckstring-records-")
    os.close(fd)
    try:
        await run_in_threadpool(records.read_parquet, str(body.get("schema", "")), str(body.get("table", "")),
                                Path(path))
    except RecordsError as exc:
        Path(path).unlink(missing_ok=True)
        raise HTTPException(status_code=404 if "no Records table" in str(exc) else 422, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/octet-stream",
                        background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)))


@router.get("/duck/{name}/{major}/media/{app}/{app_major}", dependencies=[auth.duck])
async def media_list(name: str, major: int, app: str, app_major: int, request: Request, prefix: str = ""):
    """An App line's Media listing, for a Duck that can't reach this machine's disk."""
    from ..media import MediaError

    try:
        files = await run_in_threadpool(request.app.state.driver.media.list, app, app_major, prefix)
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"files": files}


@router.get("/duck/{name}/{major}/media/{app}/{app_major}/file", dependencies=[auth.duck])
async def media_file(name: str, major: int, app: str, app_major: int, request: Request, path: str):
    from fastapi.responses import StreamingResponse

    from ..media import MediaError

    media = request.app.state.driver.media
    try:
        size = await run_in_threadpool(media.size, app, app_major, path)
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file {path!r}") from None
    return StreamingResponse(media.read(app, app_major, path), media_type="application/octet-stream",
                             headers={"Content-Length": str(size)})


@router.get("/duck/{name}/{major}/artifact", dependencies=[auth.duck])
def artifact(name: str, major: int, request: Request):
    """The selected version's source bundle for this major line, as an uncompressed tar (code is
    small; the fetch is boot-time-critical). The Duck unpacks it at its own root and proceeds —
    the registry and ledger it then builds are ephemeral caches over the published plane."""
    driver = request.app.state.driver
    meta = driver.meta.get(pond_key(name, major))
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown pond {name}@{major}")
    source_dir = Path(driver.root) / meta["source_path"]
    if not source_dir.is_dir():
        raise HTTPException(status_code=404, detail="artifact not on disk")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(source_dir, arcname=".")
    return Response(
        content=buf.getvalue(), media_type="application/x-tar",
        headers={"X-Duckstring-Version": meta["version"], "X-Duckstring-Source-Path": meta["source_path"]},
    )


@router.websocket("/duck/{name}/{major}/app/{instance}/tunnel")
async def app_tunnel(websocket: WebSocket, name: str, major: int, instance: int):
    """An App instance's agent on another machine (plans/app-scaling.md): the tunnel that carries the Catchment's
    connections to the App and the agent's control messages. Refused (403) for an instance the Catchment isn't
    running, which tells the agent to stop."""
    supervisor = getattr(websocket.app.state, "app_supervisor", None)
    allowed = auth.duck_allowed(websocket)
    session = supervisor.tunnels.get(instance) if supervisor is not None else None
    if allowed and session is None and supervisor is not None:  # one a previous Catchment process started
        session = await supervisor.adopt(instance, name, major)
    if not allowed or session is None or session.closed:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    async def receive():
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        return message.get("bytes") or b""

    try:
        await session.serve(receive, websocket.send_bytes)
    except Exception:  # the socket went: the agent dials again
        pass
