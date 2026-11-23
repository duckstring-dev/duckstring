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
    listen: str | None = None  # a managed PostgreSQL's listen address


@router.put("/catchment/records", dependencies=[auth.full])
async def put_records(request: Request, body: _RecordsBody):
    from ...core import parse_size
    from ..records import RecordsError

    records = _records(request)
    try:
        memory = parse_size(body.memory) if body.memory not in (None, "") else None
        await asyncio.to_thread(records.configure, body.target, memory, body.listen)
    except (RecordsError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {**records.status(), "in_use": records.in_use()}


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
