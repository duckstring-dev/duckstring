"""Read-only data access for Outlets, served from each Pond's **exported** Parquet snapshot
(``ponds/{pond}/data/{table}.parquet``) — the published, consistent output of a successful run — via
an in-memory DuckDB connection. It never opens the live ``registry.duckdb`` a Duck is writing to, so a
data query never contends with (or blocks) a running Pond."""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


def _resolve_major(request: Request, pond_name: str, major: Optional[int], version: Optional[str]) -> int:
    """The major line whose exported data to read: explicit ``major``, the ``version``'s major, or
    the highest deployed major of the Pond (falling back to the highest ``m*`` dir on disk — exported
    data outlives a deployment). Data is per major line, not per version."""
    if major is not None:
        return major
    if version is not None:
        return int(version.split(".")[0])
    db = request.app.state.db
    row = db.execute(
        "SELECT MAX(p.major) FROM pond p JOIN pond_name pn ON pn.id = p.pond_name_id WHERE pn.name = ?",
        (pond_name,),
    ).fetchone()
    if row is not None and row[0] is not None:
        return row[0]
    on_disk = sorted(
        int(d.name[1:])
        for d in (Path(request.app.state.root) / "ponds" / pond_name).glob("m*")
        if d.is_dir() and d.name[1:].isdigit()
    )
    if on_disk:
        return on_disk[-1]
    raise HTTPException(status_code=404, detail=f"Pond '{pond_name}' not found")


def _data_dir(request: Request, pond_name: str, major: int) -> Path:
    from ..registry import pond_data_dir

    return pond_data_dir(Path(request.app.state.root), pond_name, major)


def _open_pond(request: Request, pond_name: str, major: int):
    """An in-memory DuckDB connection with the Pond's exported tables registered as views — under a
    schema named after the Pond, and in ``main`` — so queries can name them ``"pond"."table"`` or
    bare. Reads the Parquet snapshot, not the live registry, so there is no cross-process lock."""
    import duckdb

    from ...dataplane import get_data_plane

    dp = get_data_plane()
    data_dir = _data_dir(request, pond_name, major)
    con = duckdb.connect()  # in-memory: no file, no lock, no contention
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{pond_name}"')
    for table in dp.list_tables(data_dir):
        select = dp.read_select(data_dir, table)
        con.execute(f'CREATE VIEW "{pond_name}"."{table}" AS {select}')
        con.execute(f'CREATE OR REPLACE VIEW "{table}" AS {select}')
    return con


@router.get("/ponds/{name}/versions/{version}")
def get_pond_version(name: str, version: str, request: Request):
    db = request.app.state.db
    row = db.execute(
        """SELECT pv.id FROM pond_version pv
           JOIN pond_name pn ON pn.id = pv.pond_name_id
           WHERE pn.name = ? AND pv.version = ?""",
        (name, version),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No version {version} of pond '{name}'")
    # "active" = this version is the one the pond pointer currently selects.
    selected = db.execute(
        "SELECT 1 FROM pond WHERE pond_version_id = ?", (row[0],)
    ).fetchone()
    return {"name": name, "version": version, "is_active": bool(selected)}


class QueryRequest(BaseModel):
    pond: str
    major: Optional[int] = None
    version: Optional[str] = None
    ripple: Optional[str] = None
    sql: Optional[str] = None
    format: Optional[str] = None


@router.get("/ponds/{outlet}/ripples/{ripple_name}")
def get_ripple(
    outlet: str, ripple_name: str, request: Request,
    major: Optional[int] = None, version: Optional[str] = None,
):
    # Serve the published file directly — no DuckDB needed (Parquet backend has one file per table).
    from ...dataplane import get_data_plane

    m = _resolve_major(request, outlet, major, version)
    pq = get_data_plane().table_path(_data_dir(request, outlet, m), ripple_name)
    if pq is None or not pq.exists():
        raise HTTPException(status_code=404, detail=f"No data for {outlet}.{ripple_name} (major {m})")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(pq, f"{ripple_name}.parquet")
    return Response(content=buf.getvalue(), media_type="application/zip")


def _maybe_tap_on_get(request: Request, pond: str, major: Optional[int], version: Optional[str]) -> None:
    """If the Pond is open with tap-on-get, fire a Tap (the snapshot is served first — we never block
    on freshness). Best-effort: a data query must never fail because of this."""
    driver = getattr(request.app.state, "driver", None)
    if driver is None:
        return
    try:
        key = driver.resolve(pond, major, version)
        if driver.pond_tap_on_get(key):
            driver.tap(key)
    except Exception:
        pass


@router.post("/query")
def query(body: QueryRequest, request: Request):
    _maybe_tap_on_get(request, body.pond, body.major, body.version)
    con = _open_pond(request, body.pond, _resolve_major(request, body.pond, body.major, body.version))
    sql = body.sql
    if not sql:
        if body.ripple:
            sql = f'SELECT * FROM "{body.pond}"."{body.ripple}" LIMIT 10'
        else:
            sql = f'SELECT * FROM "{body.pond}" LIMIT 10'

    fmt = (body.format or "").lower()
    try:
        if fmt:
            suffix_map = {"csv": ".csv", "json": ".json", "parquet": ".parquet"}
            media_map = {"csv": "text/csv", "json": "application/json", "parquet": "application/octet-stream"}
            with tempfile.NamedTemporaryFile(suffix=suffix_map.get(fmt, ".bin"), delete=False) as f:
                tmp_path = f.name
            try:
                con.execute(f"COPY ({sql}) TO '{tmp_path}' (FORMAT {fmt.upper()})")
                data = Path(tmp_path).read_bytes()
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            return Response(content=data, media_type=media_map.get(fmt, "application/octet-stream"))

        rel = con.execute(sql)
        if rel.description is None:
            return []
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row, strict=False)) for row in rel.fetchall()]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        con.close()
