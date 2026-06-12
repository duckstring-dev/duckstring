"""Duck protocol endpoints.

``GET  /api/duck/{name}/{major}/jobs``   — the Duck polls for queued commands (``begin_run`` / ``shutdown``).
``POST /api/duck/{name}/{major}/events`` — the Duck reports ``ripple`` / ``run_completed`` events.

A Duck serves one major line of a Pond, so both routes address the pond key ``name@major``. Both are
Duck-initiated, so the same shape works for a local subprocess or a remote Duck. Job delivery is a
short poll (the Duck re-polls on a short interval); events are idempotent on freshness.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...keys import pond_key

router = APIRouter()


@router.get("/duck/{name}/{major}/jobs")
def jobs(name: str, major: int, request: Request):
    driver = request.app.state.driver
    return {"jobs": driver.take_jobs(pond_key(name, major))}


@router.post("/duck/{name}/{major}/events")
async def events(name: str, major: int, request: Request):
    driver = request.app.state.driver
    payload = await request.json()
    driver.on_event(pond_key(name, major), payload)
    return {"ok": True}
