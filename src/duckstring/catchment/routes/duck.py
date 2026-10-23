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
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
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
