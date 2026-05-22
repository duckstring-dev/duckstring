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


class QueryRequest(BaseModel):
    pond: str
    ripple: Optional[str] = None
    sql: Optional[str] = None
    format: Optional[str] = None


@router.get("/ponds/{outlet}/ripples/{ripple_name}")
def get_ripple(outlet: str, ripple_name: str, request: Request):
    registry = request.app.state.registry

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        tmp_path = f.name

    try:
        registry.execute(
            f"COPY \"{outlet}\".\"{ripple_name}\" TO '{tmp_path}' (FORMAT PARQUET)"
        )
    except Exception as exc:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail=f"No data for {outlet}.{ripple_name}") from exc

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp_path, f"{ripple_name}.parquet")
    Path(tmp_path).unlink(missing_ok=True)

    return Response(content=buf.getvalue(), media_type="application/zip")


@router.post("/query")
def query(body: QueryRequest, request: Request):
    registry = request.app.state.registry

    sql = body.sql
    if not sql:
        if body.ripple:
            sql = f'SELECT * FROM "{body.pond}"."{body.ripple}" LIMIT 10'
        else:
            sql = f'SELECT * FROM "{body.pond}" LIMIT 10'

    fmt = body.format
    if fmt:
        fmt = fmt.lower()
        suffix_map = {"csv": ".csv", "json": ".json", "parquet": ".parquet"}
        media_map = {
            "csv": "text/csv",
            "json": "application/json",
            "parquet": "application/octet-stream",
        }
        with tempfile.NamedTemporaryFile(suffix=suffix_map.get(fmt, ".bin"), delete=False) as f:
            tmp_path = f.name
        try:
            registry.execute(f"COPY ({sql}) TO '{tmp_path}' (FORMAT {fmt.upper()})")
            data = Path(tmp_path).read_bytes()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return Response(content=data, media_type=media_map.get(fmt, "application/octet-stream"))

    try:
        rel = registry.execute(sql)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
