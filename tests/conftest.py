from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner


@pytest.fixture(autouse=True, scope="session")
def fast_demo_sleep():
    """Set env vars for the test session:
    - DUCKSTRING_SLEEP_MULTIPLIER=0.01: demo-pond sleeps become negligible
    - DUCKSTRING_DEMO_*: shrink the (deliberately large, ~50 MB) Trickle demo to a few thousand rows so
      the e2e suites stay quick — the production-scale defaults live in the demo pond code
    - NO_COLOR=1: prevent Rich/Typer from emitting ANSI codes in captured output
    """
    to_restore = {}
    for key, val in [("DUCKSTRING_SLEEP_MULTIPLIER", "0.01"), ("NO_COLOR", "1"),
                     ("DUCKSTRING_DISABLE_DUCKS", "1"),
                     ("DUCKSTRING_DEMO_ORDERS", "4000"), ("DUCKSTRING_DEMO_BATCH", "200"),
                     ("DUCKSTRING_DEMO_PRODUCTS", "500"), ("DUCKSTRING_DEMO_PRICE_CHANGES", "10")]:
        to_restore[key] = os.environ.get(key)
        os.environ[key] = val
    yield
    for key, prev in to_restore.items():
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def no_ambient_azure_token(monkeypatch):
    """Never mint a real Azure token from the machine's own login (a developer's `az login`): a test that wants
    the access-token path sets `duckstring.storage._azure_token` itself."""
    from duckstring import storage

    monkeypatch.setattr(storage, "_azure_token", lambda: None)


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
def mock_uvicorn(monkeypatch):
    """Prevent uvicorn.run from actually starting a server in unit tests."""
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)


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
def catchment_root(tmp_path_factory):
    """The filesystem root the live catchment serves from — tests write exported Parquet under
    ``{root}/ponds/{pond}/data/`` to seed the (read-only, Parquet-backed) data API."""
    return tmp_path_factory.mktemp("catchment_root")


@pytest.fixture
def live_catchment(catchment_root):
    """Start a real uvicorn server and register it as the 'dev' catchment.

    Yields the base URL. Tests can hit the API directly to verify state.
    """
    from catchment_server import serve_catchment

    from duckstring.cli.config import register_catchment, set_default_catchment

    with serve_catchment(catchment_root) as url:
        register_catchment("dev", url=url, kind="local")
        set_default_catchment("dev")
        yield url
