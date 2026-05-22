from __future__ import annotations

from duckstring.cli import app
from duckstring.cli.config import get_default_catchment, list_catchments


def test_connect_registers(runner):
    result = runner.invoke(app, ["catchment", "connect", "--name", "prod", "--path", "https://example.com", "--yes"])
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


def test_connect_yes_sets_default(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474", "--yes"])
    assert get_default_catchment() == "dev"


def test_connect_prompts_for_default(runner):
    result = runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474"], input="y\n")
    assert result.exit_code == 0
    assert get_default_catchment() == "dev"


def test_connect_decline_default(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474", "--yes"])
    runner.invoke(app, ["catchment", "connect", "--name", "prod", "--path", "https://prod.example.com"], input="n\n")
    assert get_default_catchment() == "dev"  # unchanged


def test_connect_skips_prompt_when_already_default(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474", "--yes"])
    # Second connect with same name: already default, prompt should not appear
    result = runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474"])
    assert result.exit_code == 0
    assert "Set" not in result.output


# ── auto-default with single catchment ───────────────────────────────────────


def test_single_catchment_is_implicit_default(runner, tmp_path, monkeypatch):
    """When only one catchment is registered with no explicit default, it is used automatically."""
    from duckstring.cli import app as cli_app
    from duckstring.cli.config import register_catchment

    register_catchment("solo", url="http://localhost:7474", kind="local")
    # No set_default_catchment call — solo should still resolve
    result = runner.invoke(cli_app, ["status"])
    # Will fail (no server), but must NOT fail with "no catchment" error
    assert "no catchment" not in result.output.lower()


def test_single_catchment_marker_in_list(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "solo", "--path", "http://localhost:7474"], input="n\n")
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "●" in result.output  # implicit default marked


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_empty(runner):
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "No catchments" in result.output


def test_list_shows_registered(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474", "--yes"])
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "http://localhost:7474" in result.output


def test_list_shows_multiple(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474", "--yes"])
    runner.invoke(app, ["catchment", "connect", "--name", "prod", "--path", "https://prod.example.com"], input="n\n")
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "prod" in result.output


def test_list_marks_default(runner):
    runner.invoke(app, ["catchment", "connect", "--name", "dev", "--path", "http://localhost:7474"], input="n\n")
    runner.invoke(app, ["catchment", "connect", "--name", "prod", "--path", "https://prod.example.com"], input="n\n")
    runner.invoke(app, ["catchment", "set-default", "prod"])
    result = runner.invoke(app, ["catchment", "list"])
    assert result.exit_code == 0
    assert "●" in result.output


def test_start_help(runner):
    result = runner.invoke(app, ["catchment", "start", "--help"])
    assert result.exit_code == 0
    assert "--name" in result.output
    assert "--port" in result.output
    assert "--root" in result.output
