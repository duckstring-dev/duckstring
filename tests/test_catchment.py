from __future__ import annotations

from duckstring.cli import app
from duckstring.cli.config import list_catchments


def test_connect_registers(runner):
    result = runner.invoke(app, ["catchment", "connect", "--name", "prod", "--path", "https://example.com"])
    assert result.exit_code == 0
    items = dict(list_catchments())
    assert "prod" in items
    assert items["prod"]["url"] == "https://example.com"
    assert items["prod"]["type"] == "remote"


def test_connect_requires_name(runner):
    result = runner.invoke(app, ["catchment", "connect", "--path", "https://example.com"])
    assert result.exit_code != 0


def test_connect_requires_path(runner):
    result = runner.invoke(app, ["catchment", "connect", "--name", "dev"])
    assert result.exit_code != 0


def test_list_empty(runner):
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "No catchments" in result.output


def test_list_shows_registered(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474"])
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "http://localhost:7474" in result.output


def test_list_shows_multiple(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474"])
    runner.invoke(app, ["catchment", "connect", "--name", "prod", "--path", "https://prod.example.com"])
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "prod" in result.output


def test_start_help(runner):
    result = runner.invoke(app, ["catchment", "start", "--help"])
    assert result.exit_code == 0
    assert "--name" in result.output
    assert "--port" in result.output
    assert "--root" in result.output
