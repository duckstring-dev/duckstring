from __future__ import annotations

import asyncio
import signal
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path


def _worker_init():
    # Workers inherit the terminal's process group and receive SIGINT on Ctrl+C.
    # Ignoring it here lets workers finish cleanly while the main process handles shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

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
    app.state.executor = ProcessPoolExecutor(max_workers=8, initializer=_worker_init)

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
    try:
        await task
    except asyncio.CancelledError:
        pass
    app.state.executor.shutdown(wait=False, cancel_futures=True)


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
