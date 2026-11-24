"""Records and Media (plans/records-and-media.md): where they live, and each App line's data.

Settings: ``GET /api/catchment/records|media`` (read), ``PUT`` (full).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth

router = APIRouter()


def _records(request: Request):
    return request.app.state.driver.records


def _media(request: Request):
    return request.app.state.driver.media


@router.get("/catchment/records", dependencies=[auth.read])
def get_records(request: Request):
    records = _records(request)
    return {**records.status(), "in_use": records.in_use()}


class _RecordsBody(BaseModel):
    target: str | None = None  # "managed" or a postgres:// URI
    memory: str | int | None = None  # a managed PostgreSQL's memory ("1GB")
    listen: str | None = None  # a managed PostgreSQL's listen address (beyond this machine: TLS only)
    host: str | None = None  # the address another machine reaches it at
    tls_cert: str | None = None  # the operator's certificate and key, paths on the Catchment's machine
    tls_key: str | None = None


@router.put("/catchment/records", dependencies=[auth.full])
async def put_records(request: Request, body: _RecordsBody):
    from ...core import parse_size
    from ..records import RecordsError

    records = _records(request)
    try:
        memory = parse_size(body.memory) if body.memory not in (None, "") else None
        await asyncio.to_thread(records.configure, body.target, memory, body.listen, body.host, body.tls_cert,
                                body.tls_key)
    except (RecordsError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {**records.status(), "in_use": records.in_use()}


@router.get("/catchment/records/reader", dependencies=[auth.full])
async def records_reader(request: Request):
    """A read-only connection URL for every line's Records, for a client on another machine (a BI tool): the
    managed PostgreSQL's reachable address over TLS, or the external one's. Full access: it carries a password."""
    from ..records import RecordsError

    records = _records(request)
    try:
        if not records.in_use():
            raise RecordsError("no App line holds Records yet")
        url = await asyncio.to_thread(records.reader_url, None, True, True)
    except RecordsError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"url": url, "tls": records.status().get("tls")}


@router.get("/catchment/media", dependencies=[auth.read])
def get_media(request: Request):
    media = _media(request)
    return {"target": media.target, "in_use": media.in_use()}


class _MediaBody(BaseModel):
    target: str  # "local" or an object-store URI


@router.put("/catchment/media", dependencies=[auth.full])
def put_media(request: Request, body: _MediaBody):
    from ..media import MediaError

    media = _media(request)
    try:
        media.configure(body.target)
    except MediaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"target": media.target, "in_use": media.in_use()}


# ─── App lines' data (the viewer and the catalog) ─────────────────────────────────


@router.get("/app-data", dependencies=[auth.read])
async def list_app_data(request: Request):
    """Every App line holding Records or Media, with its state, its Records tables (``records.{name}_v{major}``
    in the query catalog) and its Media's file count and size."""
    driver = request.app.state.driver
    lines = [d for d in driver.app_data() if d["records"] or d["media"]]  # a retired line that kept only a state dir
    records, media = _records(request), _media(request)
    schemas = [f"{d['name']}_v{d['major']}" for d in lines if d["records"]]
    tables: dict = {}
    error = None
    if schemas:
        try:
            tables = await asyncio.to_thread(records.tables, schemas)
        except Exception as e:  # PostgreSQL unreachable: still list the lines
            error = str(e)

    def usage(d):
        try:
            files = media.list(d["name"], d["major"])
        except Exception:
            return None
        return {"files": len(files), "bytes": sum(f["size"] for f in files)}

    out = []
    for d in lines:
        entry = {**d, "schema": f"{d['name']}_v{d['major']}"}
        if d["records"]:
            entry["tables"] = tables.get(entry["schema"], [])
        if d["media"]:
            entry["usage"] = await asyncio.to_thread(usage, d)
        out.append(entry)
    return {"lines": out, "records_error": error, "catalog": "records"}


def _line(request: Request, name: str, major: int) -> dict:
    found = next((d for d in request.app.state.driver.app_data(name) if d["major"] == major), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"{name}@{major} holds no Records or Media")
    return found


def _media_line(request: Request, name: str, major: int) -> dict:
    line = _line(request, name, major)
    if not line["media"]:
        raise HTTPException(status_code=404, detail=f"{name}@{major} has no Media")
    return line


@router.get("/app-data/{name}/{major}/media", dependencies=[auth.read])
async def list_media(request: Request, name: str, major: int, prefix: str = ""):
    from ..media import MediaError

    _media_line(request, name, major)
    try:
        files = await asyncio.to_thread(_media(request).list, name, major, prefix)
    except MediaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"files": files}


@router.get("/app-data/{name}/{major}/media/file", dependencies=[auth.read])
async def download_media(request: Request, name: str, major: int, path: str):
    from fastapi.responses import StreamingResponse

    from ..media import MediaError

    _media_line(request, name, major)
    media = _media(request)
    try:
        size = await asyncio.to_thread(media.size, name, major, path)
    except MediaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file {path!r}") from None
    import mimetypes
    from urllib.parse import quote

    filename = path.rsplit("/", 1)[-1]
    return StreamingResponse(
        media.read(name, major, path), media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        headers={"Content-Length": str(size),
                 "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


@router.post("/app-data/{name}/{major}/media/file", dependencies=[auth.full])
async def upload_media(request: Request, name: str, major: int, path: str):
    """Upload a file into the line's Media at ``path`` (the request body, or a multipart ``file``), replacing a
    file there. Manage access only, so Media can't be used as a drop box."""
    import tempfile

    from ..media import MediaError

    line = _media_line(request, name, major)
    if line["state"] == "retired":
        raise HTTPException(status_code=409, detail=f"{name}@{major} is retired: its Media is read-only")
    media = _media(request)
    content_type = request.headers.get("content-type", "")
    spool = tempfile.SpooledTemporaryFile(max_size=8 << 20)
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=422, detail="the form needs a file")
            while chunk := await upload.read(1 << 20):
                spool.write(chunk)
        else:
            async for chunk in request.stream():
                spool.write(chunk)
        spool.seek(0)
        written = await asyncio.to_thread(media.write, name, major, path, spool)
    except MediaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    finally:
        spool.close()
    return {"path": path, "size": written}


@router.delete("/app-data/{name}/{major}/media/file", dependencies=[auth.full])
async def delete_media(request: Request, name: str, major: int, path: str):
    from ..media import MediaError

    line = _media_line(request, name, major)
    if line["state"] == "retired":
        raise HTTPException(status_code=409, detail=f"{name}@{major} is retired: its Media is read-only")
    try:
        await asyncio.to_thread(_media(request).delete, name, major, path)
    except MediaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file {path!r}") from None
    return {"deleted": path}
