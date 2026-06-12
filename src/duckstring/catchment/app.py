from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
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
        # Ducks dial back over the same authenticated surface — they present the API key as their token.
        launcher = SubprocessLauncher(app.state.root, base_url, token=app.state.api_key or "")
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


def create_app(root: Path, api_key: str | None = None) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    con = connect(root / "duck.db")
    migrate(con)

    app = FastAPI(title="Duckstring Catchment", lifespan=_lifespan)
    app.state.root = root
    app.state.db = con
    # API key: explicit argument, or the environment (useful for containers/remote serving). When
    # set, every /api request (except /api/health) must present it — Bearer header or X-Duck-Token.
    app.state.api_key = api_key or os.environ.get("DUCKSTRING_API_KEY") or None

    @app.middleware("http")
    async def _require_api_key(request, call_next):
        key = app.state.api_key
        path = request.url.path
        if key and path.startswith("/api") and path != "/api/health":
            auth = request.headers.get("authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
            supplied = supplied or request.headers.get("x-duck-token", "")
            if not secrets.compare_digest(supplied, key):
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        return await call_next(request)

    app.include_router(router, prefix="/api")

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")

    return app
