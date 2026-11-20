"""A real Catchment served over HTTP for tests (an importable module: ``conftest`` is ambiguous across the test
trees)."""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import httpx
import uvicorn


@contextlib.contextmanager
def serve_catchment(root):
    """A real uvicorn server for a Catchment at ``root``, on a free port. Yields its base URL."""
    from duckstring.catchment.app import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="error")
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
