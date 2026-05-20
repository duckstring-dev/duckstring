from __future__ import annotations

from unittest.mock import MagicMock

import pytest
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
    """Register a 'dev' catchment pointing at localhost."""
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
