from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import connect, migrate
from .routes import router

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from .orchestrator import sentinel_loop

    app.state.db_path = app.state.root / "duck.db"
    app.state.sentinel_queue = asyncio.Queue()
    app.state.executor = ProcessPoolExecutor(max_workers=8)

    task = asyncio.create_task(
        sentinel_loop(
            app.state.sentinel_queue,
            app.state.db_path,
            app.state.root,
            app.state.executor,
        )
    )
    yield
    task.cancel()
    app.state.executor.shutdown(wait=False)


def create_app(root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    con = connect(root / "duck.db")
    migrate(con)

    app = FastAPI(title="Duckstring Catchment", lifespan=_lifespan)
    app.state.root = root
    app.state.db = con

    app.include_router(router, prefix="/api")

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")

    return app
