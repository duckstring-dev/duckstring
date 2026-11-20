"""A real Catchment served over HTTP for tests (an importable module: ``conftest`` is ambiguous across the test
trees)."""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import httpx
import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve_catchment(root, name: str | None = None, port: int | None = None):
    """A real uvicorn server for a Catchment at ``root`` (named ``name``), on ``port`` or a free one. Yields its
    base URL."""
    from duckstring.catchment.app import create_app

    port = port or free_port()
    config = uvicorn.Config(create_app(root, name=name), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"Catchment did not start on port {port}")
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
