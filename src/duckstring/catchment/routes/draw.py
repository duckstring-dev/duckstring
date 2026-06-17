"""The draw route — the cross-Catchment data-transfer channel.

A downstream Catchment's poller fetches a producing Pond's full exported Parquet over this route and
lands it in its own landing zone (`ponds/{name}/m{major}/data/`). It streams the **raw** Parquet
files (a zip), unlike `/api/query` which runs SQL. Reads off the exported snapshot, never the live
registry, so it never contends with a running Duck. tap-on-get lives on `/api/query`, not here —
so a Catchment draw never triggers it.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .data import _data_dir, _resolve_major

router = APIRouter()

_WAIT_TICK = 0.1  # how often the long-poll re-checks the Pond's freshness


@router.get("/draw/{name}/{major}/wait")
async def draw_wait(
    name: str, major: int, request: Request,
    after: Optional[str] = None, down: bool = False, timeout: float = 20.0,
):
    """Long-poll: block until this Pond line's freshness advances past ``after``, or its down-state
    *changes* from ``down`` (the consumer passes the state it already knows), or ``timeout``, then
    return ``{end_f, down}``. A downstream Catchment's poller holds this so a Draw transfers the
    instant the upstream is fresh — no poll-interval latency. Returning on a down **transition** (not
    a persistent down) is what stops the poller spinning when an upstream is durably blocked. Dial-back
    preserved: the consumer holds the connection, the producer never calls back."""
    driver = getattr(request.app.state, "driver", None)
    if driver is None:
        raise HTTPException(status_code=503, detail="driver not ready")
    try:
        key = driver.resolve(name, major, None)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc)) from exc

    after_dt = datetime.fromisoformat(after) if after else None
    for _ in range(max(1, int(timeout / _WAIT_TICK))):
        obs = driver.pond_observation(key)
        end_f = obs["end_f"]
        advanced = end_f is not None and (after_dt is None or datetime.fromisoformat(end_f) > after_dt)
        if advanced or obs["down"] != down:
            return obs
        await asyncio.sleep(_WAIT_TICK)
    return driver.pond_observation(key)


@router.get("/draw/{name}/{major}")
def draw(name: str, major: int, request: Request, tables: Optional[str] = None):
    """Stream all of a Pond line's exported Parquet as a zip. ``tables`` (comma-separated) optionally
    restricts the set — reserved for per-Ripple duct scope; default is every table."""
    m = _resolve_major(request, name, major, None)
    data_dir = _data_dir(request, name, m)
    wanted = {t.strip() for t in tables.split(",")} if tables else None

    files = sorted(p for p in data_dir.glob("*.parquet") if wanted is None or p.stem in wanted)
    if not files and wanted is None and not data_dir.exists():
        raise HTTPException(status_code=404, detail=f"No exported data for '{name}' (major {m})")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pq in files:
            zf.write(pq, pq.name)
        # The Trickle mode/PK sidecar travels with the data so the consuming Catchment's read_delta can
        # resolve a Trickle source (mode/PK aren't in the downstream's duck.db). Harmless for plain Ponds.
        from ...trickle_io import SIDECAR

        sidecar = data_dir / SIDECAR
        if sidecar.exists():
            zf.write(sidecar, sidecar.name)
    return Response(content=buf.getvalue(), media_type="application/zip")
