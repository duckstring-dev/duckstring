"""Serving an App locally against its Puddles (plans/apps.md): ``duckstring pond run`` in an App's directory.

The App's process gets the same environment the Catchment gives it, with its handle rooted at ``puddles/`` (so
``read_table("sales.revenue")`` reads the Puddle), its state directory at ``puddles/app/state/`` and an empty base
path, so it serves at ``http://127.0.0.1:{port}/``. A static-only App is served with Python's own file server; an
App with both a command and ``static`` gets a front server that serves the files and forwards the rest, as the
Catchment does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from ..app import ENV, app_config
from .project import Project


def app_env(project: Project, port: int) -> dict[str, str]:
    cfg = app_config(project.info, project.dir)
    env = dict(os.environ)
    # As on the Catchment: the environment's bin first, so a command it installs (streamlit, gunicorn) is found.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["PORT"] = str(port)
    env["DUCKSTRING_APP_PATH"] = ""
    env["PYTHONUNBUFFERED"] = "1"
    if cfg["state"] == "local":
        state = project.puddles_dir / "app" / "state"
        state.mkdir(parents=True, exist_ok=True)
        env["DUCKSTRING_APP_STATE"] = str(state)
    env[ENV] = json.dumps({"name": project.name, "major": int(project.version.split(".")[0]),
                           "version": project.version, "root": str(project.puddles_dir),
                           "sources": list(project.sources), "source_dirs": _source_dirs(project)})
    return env


def _source_dirs(project: Project) -> dict[str, str]:
    """Where the App reads each Source that isn't in the flat ``puddles/ponds/`` layout: a live Puddle in place, a
    working copy a step or live definition wrote."""
    from .runner import _Plan
    from .state import load_hydrated

    hydrated = load_hydrated(project)
    if hydrated is None:
        return {}
    plan = _Plan(project, hydrated)
    dirs = {}
    for source, state in hydrated.sources.items():
        store = plan.store(source, state)
        if store is not None:
            dirs[source] = store.uri()
    return dirs


def serve_app(project: Project, port: int) -> int:
    """Run the App in the foreground until it exits or is interrupted. Returns its exit code.

    With both ``command`` and ``static``, the command listens on a port of its own and a front server on ``port``
    does what the Catchment does: files under ``static`` are served directly, everything else (WebSockets
    included) goes to the command."""
    cfg = app_config(project.info, project.dir)
    if cfg["command"] and cfg["static"]:
        return _serve_fronted(project, cfg, port)
    env = app_env(project, port)
    if cfg["command"]:
        argv = ["/bin/sh", "-c", "exec " + cfg["command"]]
    else:
        argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                "--directory", str(project.dir / cfg["static"])]
    proc = subprocess.Popen(argv, cwd=project.dir, env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        return _stop(proc)


def _stop(proc: subprocess.Popen) -> int:
    proc.terminate()
    try:
        return proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve_fronted(project: Project, cfg: dict, port: int) -> int:
    import uvicorn

    inner = _free_port()
    proc = subprocess.Popen(["/bin/sh", "-c", "exec " + cfg["command"]], cwd=project.dir,
                            env=app_env(project, inner))
    server = uvicorn.Server(uvicorn.Config(front_app(project.dir / cfg["static"], inner), host="127.0.0.1",
                                           port=port, log_level="warning"))

    def watch() -> None:  # the command exiting ends the run, as it does without a front server
        proc.wait()
        server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    try:
        server.run()  # until Ctrl-C (uvicorn's own handler) or the command exits
    finally:
        code = proc.returncode if proc.poll() is not None else _stop(proc)
    return code


def front_app(static_dir: Path, inner: int):
    """The front server for an App with both ``command`` and ``static``: ``static`` files (``Cache-Control:
    no-cache``, as the Catchment sends), else the request forwarded unchanged to the command on port ``inner``.
    The base path is empty locally, so there is nothing to strip."""
    from contextlib import asynccontextmanager

    import httpx
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.responses import FileResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute

    from ..app import static_file
    from ..catchment.app_proxy import _HOP, _pump

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0), follow_redirects=False) as client:
            app.state.client = client
            yield

    def target(conn, scheme: str) -> str:
        raw = (conn.scope.get("raw_path") or conn.url.path.encode()).decode("latin-1")
        query = conn.scope.get("query_string", b"").decode("latin-1")
        return f"{scheme}://127.0.0.1:{inner}{raw}" + (f"?{query}" if query else "")

    async def forward(request):
        if request.method in ("GET", "HEAD"):
            found = static_file(static_dir, request.path_params["path"])
            if found is not None:
                return FileResponse(found, headers={"Cache-Control": "no-cache"})
        client = request.app.state.client
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _HOP]
        upstream = client.build_request(request.method, target(request, "http"), headers=headers,
                                        content=request.stream())
        try:
            resp = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            return Response(f"the App's command didn't answer on port {inner}: {type(exc).__name__}\n",
                            status_code=502, media_type="text/plain")
        out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
        out.raw_headers = [(k.lower(), v) for k, v in resp.headers.raw if k.decode("latin-1").lower() not in _HOP]
        return out

    async def forward_websocket(websocket):
        import websockets

        headers = [(k, v) for k, v in websocket.headers.items()
                   if k.lower() not in _HOP and k.lower() != "host" and not k.lower().startswith("sec-websocket-")]
        try:
            upstream = await websockets.connect(target(websocket, "ws"), additional_headers=headers,
                                                subprotocols=websocket.scope.get("subprotocols") or None,
                                                max_size=None, ping_interval=None, open_timeout=10)
        except Exception:  # noqa: BLE001 - the command refused or failed the handshake
            await websocket.close(code=1011)
            return
        async with upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            await _pump(websocket, upstream, _no_message_hook)

    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    return Starlette(routes=[Route("/{path:path}", forward, methods=methods),
                             WebSocketRoute("/{path:path}", forward_websocket)], lifespan=lifespan)


async def _no_message_hook() -> None:
    """``_pump``'s per-message hook: the Catchment renews read leases and runs Taps there; locally neither exists."""
