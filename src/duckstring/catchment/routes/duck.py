"""Duck protocol endpoints.

``GET  /api/duck/{pond}/jobs``   — the Duck polls for queued commands (``begin_run`` / ``shutdown``).
``POST /api/duck/{pond}/events`` — the Duck reports ``ripple`` / ``run_completed`` events.

Both are Duck-initiated, so the same shape works for a local subprocess or a remote Duck. Job
delivery is a short poll (the Duck re-polls on a short interval); events are idempotent on freshness.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/duck/{pond}/jobs")
def jobs(pond: str, request: Request):
    driver = request.app.state.driver
    return {"jobs": driver.take_jobs(pond)}


@router.post("/duck/{pond}/events")
async def events(pond: str, request: Request):
    driver = request.app.state.driver
    payload = await request.json()
    driver.on_event(pond, payload)
    return {"ok": True}
