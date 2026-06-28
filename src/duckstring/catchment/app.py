from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth
from .db import connect, ensure_identity, migrate
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
    base_url = app.state.base_url
    if os.environ.get("DUCKSTRING_DISABLE_DUCKS"):
        launcher = NoopLauncher()
    else:
        # Ducks dial back over the duck channel with the internal worker token (decoupled from the user
        # keys, so rotating those never disrupts running Ducks).
        # base_url None = unknown (the platform picked the bind address): the launcher defers spawns
        # until the dial-back middleware learns the address from the first request.
        launcher = SubprocessLauncher(app.state.root, base_url, token=app.state.duck_token)
    driver = Driver(app.state.db, app.state.root, base_url, launcher)
    app.state.driver = driver
    app.state.launcher = launcher

    # Restore: resume any Pond Runs that were in flight when the Catchment last stopped.
    driver.resume_incomplete()

    from .egress_worker import run_egress_worker
    from .poller import run_poller

    # The poller wakes immediately when a Draw acquires demand (so it solicits its upstream at once),
    # instead of waiting for its next cycle. The driver signals across threads via the running loop.
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    driver.set_notify(lambda: loop.call_soon_threadsafe(wake.set))

    # The egress worker wakes when a Pond publishes (its Spouts may have work) or a Spout is resynced.
    egress_wake = asyncio.Event()
    driver.set_egress_notify(lambda: loop.call_soon_threadsafe(egress_wake.set))

    scheduler = asyncio.create_task(_scheduler(driver))
    poller = asyncio.create_task(run_poller(driver, app.state.root, wake))
    egress = asyncio.create_task(run_egress_worker(driver, app.state.root, egress_wake))
    try:
        yield
    finally:
        for task in (scheduler, poller, egress):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        launcher.shutdown_all()


def create_app(
    root: Path, api_key: str | None = None, base_url: str | None = None, name: str | None = None
) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    con = connect(root / "duck.db")
    migrate(con)
    ensure_identity(con, name or os.environ.get("DUCKSTRING_CATCHMENT_NAME"))

    app = FastAPI(title="Duckstring Catchment", lifespan=_lifespan)
    app.state.root = root
    app.state.db = con
    # Built-in single API key (legacy / bare self-hosting): explicit argument or the environment. It
    # means full access. The tiered read/demand/full keys live in `catchment_key` (see auth.py); either
    # may gate the API. With neither configured, the Catchment is fully open.
    app.state.api_key = api_key or os.environ.get("DUCKSTRING_API_KEY") or None
    # The internal token Ducks present on the duck channel (persisted, decoupled from the user keys).
    app.state.duck_token = auth.ensure_duck_token(con)
    # The address Ducks dial back to: explicit argument (the CLI passes its bind address), or the
    # environment, or None — unknown, because the host platform picks the bind address (e.g. Posit
    # Connect). When None it is learned from the first request's ASGI scope below.
    app.state.base_url = base_url or os.environ.get("DUCKSTRING_CATCHMENT_URL") or None

    @app.middleware("http")
    async def _learn_dialback_address(request, call_next):
        launcher = getattr(app.state, "launcher", None)
        if launcher is not None and getattr(launcher, "base_url", "") is None:
            server = request.scope.get("server")  # the server's bound (host, port) per the ASGI spec
            if server and server[1]:  # a unix socket has port None — nothing TCP to dial
                host = "127.0.0.1" if server[0] in ("0.0.0.0", "::") else server[0]
                url = f"http://{host}:{server[1]}"
                app.state.base_url = url
                app.state.driver.base_url = url
                launcher.set_base_url(url)  # spawns any Ducks that were waiting on the address
        return await call_next(request)

    app.include_router(router, prefix="/api")
    auth.audit_routes(app)  # fail-closed: every /api route must declare an access level

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")

    return app
