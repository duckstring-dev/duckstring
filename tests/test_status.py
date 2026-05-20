from __future__ import annotations

from duckstring.cli import app


def test_status_calls_api(runner, dev_catchment, mock_get):
    mock_get.return_value.json.return_value = {"ponds": []}
    result = runner.invoke(app, ["status", "dev"])
    assert result.exit_code == 0
    mock_get.assert_called_once()
    assert "/api/status" in mock_get.call_args.args[0]


def test_status_no_all_flag_by_default(runner, dev_catchment, mock_get):
    mock_get.return_value.json.return_value = {"ponds": []}
    runner.invoke(app, ["status", "dev"])
    params = mock_get.call_args.kwargs.get("params", {})
    assert "all" not in params


def test_status_all_flag_sends_param(runner, dev_catchment, mock_get):
    mock_get.return_value.json.return_value = {"ponds": []}
    result = runner.invoke(app, ["status", "dev", "--all"])
    assert result.exit_code == 0
    params = mock_get.call_args.kwargs.get("params", {})
    assert params.get("all") == "true"


def test_status_empty_ponds_message(runner, dev_catchment, mock_get):
    mock_get.return_value.json.return_value = {"ponds": []}
    result = runner.invoke(app, ["status", "dev"])
    assert result.exit_code == 0
    assert "No active" in result.output


def test_status_renders_pond_table(runner, dev_catchment, mock_get):
    mock_get.return_value.json.return_value = {
        "ponds": [
            {"name": "outlet", "version": "1.0.0", "status": "running", "demand": "wave", "last_run": "2024-01-01"},
        ]
    }
    result = runner.invoke(app, ["status", "dev"])
    assert result.exit_code == 0
    assert "outlet" in result.output
    assert "1.0.0" in result.output
    assert "running" in result.output


def test_status_unknown_catchment_exits(runner, mock_get):
    result = runner.invoke(app, ["status", "nonexistent"])
    assert result.exit_code != 0
    assert mock_get.call_count == 0
