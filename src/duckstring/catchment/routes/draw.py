"""The draw route — the cross-Catchment data-transfer channel.

A downstream Catchment's poller fetches a producing Pond's full exported Parquet over this route and
lands it in its own landing zone (`ponds/{name}/m{major}/data/`). It streams the **raw** Parquet
files (a zip), unlike `/api/query` which runs SQL. Reads off the exported snapshot, never the live
registry, so it never contends with a running Duck. tap-on-get lives on `/api/query`, not here —
so a Catchment draw never triggers it.
"""

from __future__ import annotations

import io
import zipfile
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .data import _data_dir, _resolve_major

router = APIRouter()


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
    return Response(content=buf.getvalue(), media_type="application/zip")
