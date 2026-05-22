import sqlite3

from fastapi import APIRouter, Request

router = APIRouter()


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


@router.get("/health")
def health(request: Request):
    _db(request).execute("SELECT 1")
    return {"status": "ok"}
