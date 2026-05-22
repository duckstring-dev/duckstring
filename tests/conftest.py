from __future__ import annotations

import socket
import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect ~/.duckstring/config.toml to a per-test temp directory."""
    cfg_dir = tmp_path / ".duckstring"
    cfg_file = cfg_dir / "config.toml"
    monkeypatch.setattr("duckstring.cli.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("duckstring.cli.config.CONFIG_FILE", cfg_file)


@pytest.fixture
def dev_catchment():
    """Register a 'dev' catchment pointing at a non-existent local address (for tests that fail before HTTP)."""
    from duckstring.cli.config import register_catchment

    register_catchment("dev", url="http://localhost:7474", kind="local")


@pytest.fixture
def mock_post(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("duckstring.cli._http.post", mock)
    return mock


@pytest.fixture
def mock_get(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("duckstring.cli._http.get", mock)
    return mock


@pytest.fixture
def catchment_client(tmp_path):
    from duckstring.catchment.app import create_app

    with TestClient(create_app(tmp_path)) as client:
        yield client


@pytest.fixture
def live_catchment(tmp_path_factory):
    """Start a real uvicorn server and register it as the 'dev' catchment.

    Yields the base URL. Tests can hit the API directly to verify state.
    """
    from duckstring.catchment.app import create_app
    from duckstring.cli.config import register_catchment

    root = tmp_path_factory.mktemp("catchment_root")

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

    from duckstring.cli.config import set_default_catchment

    register_catchment("dev", url=url, kind="local")
    set_default_catchment("dev")

    yield url

    server.should_exit = True
    thread.join(timeout=5)
