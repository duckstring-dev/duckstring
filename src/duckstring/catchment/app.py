from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import registry as reg
from .db import connect, migrate
from .routes import router

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    con = connect(root / "duck.db")
    migrate(con)

    app = FastAPI(title="Duckstring Catchment")
    app.state.root = root
    app.state.db = con
    app.state.registry = reg.connect(root / "registry.duckdb")

    app.include_router(router, prefix="/api")

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")

    return app
