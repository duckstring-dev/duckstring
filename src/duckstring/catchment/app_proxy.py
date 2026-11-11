"""Forwarding requests to Apps (plans/apps.md), mounted at ``/apps`` beside the API.

``/apps/{name}/v{major}/...`` reaches that major line of the App; ``/apps/{name}/...`` redirects to the App's
served major (``pond promote`` moves it). Requests arrive at the App with that base path (``DUCKSTRING_APP_PATH``)
removed and named in ``X-Forwarded-Prefix``, or with ``[app] strip_path = false``, unchanged. Files under the
App's ``static`` directory are served by the Catchment itself; anything else goes to the App's process, started on
demand by the :class:`AppSupervisor`. WebSocket connections are forwarded too.

Access is the Catchment's own gate: open when the Catchment has no keys (or a hosting platform gates it), else a
read-level key, as a bearer token or the ``duckstring_key`` cookie the sign-in form sets. The key is removed
before the request reaches the App.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..app import base_path
from ..keys import pond_key
from . import auth
from .app_supervisor import AppUnavailable

router = APIRouter()

COOKIE = "duckstring_key"
# Hop-by-hop headers (RFC 9110 §7.6.1), never forwarded either way.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
        "trailer", "transfer-encoding", "upgrade"}
_MAJOR = re.compile(r"v(\d+)(?:/(.*))?", re.S)


# ─── access ────────────────────────────────────────────────────────────────────


def _bearer(conn) -> str:
    value = conn.headers.get("authorization", "")
    return value[7:] if value.lower().startswith("bearer ") else ""


def _allowed(conn) -> tuple[bool, bool]:
    """(may this request reach an App, did it authenticate with a bearer token to strip)."""
    state = conn.app.state
    con = getattr(state, "db", None)
    api_key = getattr(state, "api_key", None)
    if not auth.auth_configured(con, api_key):
        return True, False
    bearer = _bearer(conn)
    if bearer and auth.level_for_key(con, api_key, bearer) is not None:
        return True, True
    cookie = conn.cookies.get(COOKIE, "")
    return bool(cookie) and auth.level_for_key(con, api_key, cookie) is not None, False


_SIGN_IN = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sign in</title><style>body{{font:16px system-ui,sans-serif;max-width:24rem;margin:15vh auto;padding:0 16px}}
input{{width:100%;box-sizing:border-box;padding:.5rem;margin:.5rem 0;font:inherit}}button{{padding:.5rem 1rem;font:inherit}}
.err{{color:#b00020}}</style></head><body><h1>Sign in</h1>
<p>This App is served by a Duckstring Catchment. Enter an API key with read access.</p>{error}
<form method="post" action="/apps/_signin"><input type="hidden" name="next" value="{next}">
<input type="password" name="key" autocomplete="current-password" placeholder="API key" autofocus required>
<button type="submit">Sign in</button></form></body></html>"""


def _sign_in_page(next_url: str, error: str = "", status: int = 401) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(_SIGN_IN.format(next=html.escape(next_url, quote=True), error=err), status_code=status)


@router.post("/apps/_signin")
async def sign_in(request: Request, key: str = Form(""), next: str = Form("/apps/")):
    target = next if next.startswith("/apps/") and not next.startswith("//") else "/apps/"
    state = request.app.state
    if auth.level_for_key(getattr(state, "db", None), getattr(state, "api_key", None), key) is None:
        return _sign_in_page(target, "That key isn't valid here.")
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(COOKIE, key, path="/apps/", httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return response


def _denied(request: Request) -> Response:
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return _sign_in_page(target)
    return Response("Invalid or missing API key", status_code=401, media_type="text/plain")


# ─── routing ───────────────────────────────────────────────────────────────────


def _resolve(rest: str) -> tuple[int | None, str | None]:
    """(the major this path names, the rest of the path below it). ``(None, None)`` for a bare path."""
    m = _MAJOR.fullmatch(rest)
    if m is None:
        return None, None
    return int(m.group(1)), m.group(2)


def _unavailable(exc: AppUnavailable) -> Response:
    return Response(exc.message + "\n", status_code=exc.status, media_type="text/plain")


@router.api_route("/apps/{name}", methods=["GET", "HEAD"])
async def app_root(name: str):
    return RedirectResponse(f"/apps/{quote(name)}/", status_code=307)


@router.api_route("/apps/{name}/{rest:path}",
                  methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def forward(request: Request, name: str, rest: str):
    ok, strip_bearer = _allowed(request)
    if not ok:
        return _denied(request)
    driver = request.app.state.driver
    major, below = _resolve(rest)
    if major is None:  # the bare path: follow the served major
        served = driver.app_major(name)
        if served is None or driver.app_view(pond_key(name, served)) is None:
            return Response("no such App\n", status_code=404, media_type="text/plain")
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/apps/{quote(name)}/v{served}/{rest}{query}", status_code=307)
    if below is None:  # /apps/{name}/v1 → /apps/{name}/v1/
        return RedirectResponse(f"/apps/{quote(name)}/v{major}/", status_code=307)
    key = pond_key(name, major)
    view = driver.app_view(key)
    if view is None:
        return Response("no such App\n", status_code=404, media_type="text/plain")

    static = _static_file(request.app.state.root, view, below)
    if static is not None and request.method in ("GET", "HEAD"):
        return FileResponse(static)

    supervisor = request.app.state.app_supervisor
    try:
        inst = await supervisor.acquire(key)
    except AppUnavailable as exc:
        return _unavailable(exc)
    await _run_taps(driver, key)
    return await _forward_http(request, supervisor, inst, strip_bearer, view)


async def _run_taps(driver, key: str) -> None:
    """A request reaches the App's process: its Taps may send it a Refresh (the lock only when it has some)."""
    if driver.app_has_taps(key):
        await asyncio.to_thread(driver.app_request, key)


def _static_file(root: Path, view: dict, below: str) -> Path | None:
    """The file under the App's ``static`` directory this path names (``index.html`` for a directory)."""
    static = view["app"].get("static")
    if not static:
        return None
    base = (Path(root) / view["source_path"] / static).resolve()
    target = (base / below).resolve()
    if target != base and base not in target.parents:
        return None
    if target.is_dir():
        target = target / "index.html"
    return target if target.is_file() else None


def _upstream_path(conn, view: dict) -> str:
    """The path the App receives: the raw request path, less the base path unless ``strip_path = false``."""
    raw = conn.scope.get("raw_path") or conn.url.path.encode()
    raw = raw.decode("latin-1")
    if not view["app"]["strip_path"]:
        return raw
    parts = raw.split("/", 4)  # "", "apps", name, "v{major}", rest
    return "/" + (parts[4] if len(parts) == 5 else "")


def _request_headers(conn, strip_bearer: bool, prefix: str) -> list[tuple[str, str]]:
    out = []
    for k, v in conn.headers.items():
        lk = k.lower()
        if lk in _HOP or (lk == "authorization" and strip_bearer):
            continue
        if lk == "cookie":
            v = "; ".join(c for c in v.split("; ") if not c.startswith(f"{COOKIE}="))
            if not v:
                continue
        out.append((k, v))
    client = conn.client.host if conn.client else ""
    prior = conn.headers.get("x-forwarded-for")
    out.append(("x-forwarded-for", f"{prior}, {client}" if prior else client))
    out.append(("x-forwarded-proto", conn.url.scheme.replace("ws", "http")))
    out.append(("x-forwarded-host", conn.headers.get("host", "")))
    out.append(("x-forwarded-prefix", prefix))
    return out


async def _forward_http(request: Request, supervisor, inst, strip_bearer: bool, view: dict) -> Response:
    import httpx

    client: httpx.AsyncClient = request.app.state.app_http
    query = request.scope.get("query_string", b"").decode("latin-1")
    url = f"http://127.0.0.1:{inst.port}{_upstream_path(request, view)}" + (f"?{query}" if query else "")
    headers = _request_headers(request, strip_bearer, base_path(view["name"], view["major"]))
    upstream = client.build_request(request.method, url, headers=headers, content=request.stream())
    ticket = supervisor.begin(inst)  # until the response body ends: a streamed body may still be reading
    done = False

    async def finish():
        nonlocal done
        if not done:
            done = True
            supervisor.end(inst, ticket)
            await resp.aclose()

    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        supervisor.end(inst, ticket)
        return Response(f"the App didn't answer: {type(exc).__name__}\n", status_code=502, media_type="text/plain")

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await finish()

    out = StreamingResponse(body(), status_code=resp.status_code, background=BackgroundTask(finish))
    out.raw_headers = [(k.lower(), v) for k, v in resp.headers.raw if k.decode("latin-1").lower() not in _HOP]
    return out


# ─── WebSockets ────────────────────────────────────────────────────────────────


@router.websocket("/apps/{name}/{rest:path}")
async def forward_websocket(websocket: WebSocket, name: str, rest: str):
    import websockets

    ok, strip_bearer = _allowed(websocket)
    if not ok:
        await websocket.close(code=1008)
        return
    driver = websocket.app.state.driver
    major, below = _resolve(rest)
    if major is None or below is None:  # a WebSocket can't follow a redirect
        await websocket.close(code=1008)
        return
    key = pond_key(name, major)
    view = driver.app_view(key)
    if view is None:
        await websocket.close(code=1008)
        return
    supervisor = websocket.app.state.app_supervisor
    try:
        inst = await supervisor.acquire(key)
    except AppUnavailable:
        await websocket.close(code=1013)
        return
    await _run_taps(driver, key)
    query = websocket.scope.get("query_string", b"").decode("latin-1")
    url = f"ws://127.0.0.1:{inst.port}{_upstream_path(websocket, view)}" + (f"?{query}" if query else "")
    headers = [(k, v) for k, v in _request_headers(websocket, strip_bearer, base_path(name, major))
               if k.lower() != "host" and not k.lower().startswith("sec-websocket-")]
    subprotocols = websocket.scope.get("subprotocols") or None
    ticket = supervisor.begin(inst)
    last_tap = time.monotonic()

    async def on_message():
        # A message from the browser is a use: renew the read lease, and run the Taps at most once a second.
        nonlocal last_tap
        supervisor.renew(inst, ticket)
        if time.monotonic() - last_tap >= 1.0:
            last_tap = time.monotonic()
            await _run_taps(driver, key)

    try:
        try:
            upstream = await websockets.connect(url, additional_headers=headers, subprotocols=subprotocols,
                                                max_size=None, ping_interval=None, open_timeout=10)
        except Exception:  # noqa: BLE001 - the App refused or failed the handshake
            await websocket.close(code=1011)
            return
        async with upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            await _pump(websocket, upstream, on_message)
    finally:
        supervisor.end(inst, ticket)


async def _pump(websocket: WebSocket, upstream, on_message) -> None:
    from websockets.exceptions import ConnectionClosed

    async def client_to_app():
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                await upstream.close(code=msg.get("code", 1000))
                return
            await on_message()
            if msg.get("text") is not None:
                await upstream.send(msg["text"])
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def app_to_client():
        try:
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)
        except ConnectionClosed:
            pass
        code = upstream.close_code or 1000
        try:
            await websocket.close(code=code if code != 1005 else 1000)
        except RuntimeError:
            pass  # the client already went

    tasks = [asyncio.create_task(client_to_app()), asyncio.create_task(app_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
