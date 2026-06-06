from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import connect, migrate
from .driver import Driver
from .launcher import NoopLauncher, SubprocessLauncher
from .routes import router

_STATIC_DIR = Path(__file__).parent / "static"


async def _scheduler(driver: Driver) -> None:
    """Drive clock processes (Tide deadlines, window boundaries, Wave-on-idle) at next_wake."""
    while True:
        nw = driver.next_wake()
        now = datetime.now(timezone.utc)
        delay = (nw - now).total_seconds() if nw else 1.0
        await asyncio.sleep(max(0.05, min(delay, 5.0)))
        try:
            driver.scheduler_tick()
        except Exception as exc:  # keep the loop alive
            print(f"[catchment] scheduler error: {exc}", flush=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    base_url = os.environ.get("DUCKSTRING_CATCHMENT_URL", "http://127.0.0.1:7474")
    if os.environ.get("DUCKSTRING_DISABLE_DUCKS"):
        launcher = NoopLauncher()
    else:
        launcher = SubprocessLauncher(app.state.root, base_url)
    driver = Driver(app.state.db, app.state.root, base_url, launcher)
    app.state.driver = driver
    app.state.launcher = launcher

    # Restore: resume any Pond Runs that were in flight when the Catchment last stopped.
    driver.resume_incomplete()

    scheduler = asyncio.create_task(_scheduler(driver))
    try:
        yield
    finally:
        scheduler.cancel()
        try:
            await scheduler
        except asyncio.CancelledError:
            pass
        launcher.shutdown_all()


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
