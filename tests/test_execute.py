from __future__ import annotations

from duckstring.cli import app

# ── pulse ─────────────────────────────────────────────────────────────────────


def test_pulse_calls_correct_endpoint(runner, dev_catchment, mock_post):
    result = runner.invoke(app, ["pulse", "dev", "outlet"])
    assert result.exit_code == 0
    mock_post.assert_called_once()
    assert "/api/outlets/outlet/pulse" in mock_post.call_args.args[0]


def test_pulse_empty_payload_by_default(runner, dev_catchment, mock_post):
    runner.invoke(app, ["pulse", "dev", "outlet"])
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {}


def test_pulse_with_version(runner, dev_catchment, mock_post):
    result = runner.invoke(app, ["pulse", "dev", "outlet", "--version", "2"])
    assert result.exit_code == 0
    payload = mock_post.call_args.kwargs["json"]
    assert payload["version"] == 2


def test_pulse_unknown_catchment_exits(runner, mock_post):
    result = runner.invoke(app, ["pulse", "nonexistent", "outlet"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0


# ── wave ──────────────────────────────────────────────────────────────────────


def test_wave_calls_correct_endpoint(runner, dev_catchment, mock_post):
    result = runner.invoke(app, ["wave", "dev", "outlet"])
    assert result.exit_code == 0
    mock_post.assert_called_once()
    assert "/api/outlets/outlet/wave" in mock_post.call_args.args[0]


def test_wave_unknown_catchment_exits(runner, mock_post):
    result = runner.invoke(app, ["wave", "nonexistent", "outlet"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0


# ── tide ──────────────────────────────────────────────────────────────────────


def test_tide_calls_correct_endpoint(runner, dev_catchment, mock_post):
    result = runner.invoke(app, ["tide", "dev", "outlet", "--cron", "15 2 * * *"])
    assert result.exit_code == 0
    mock_post.assert_called_once()
    assert "/api/outlets/outlet/tide" in mock_post.call_args.args[0]


def test_tide_sends_cron_expression(runner, dev_catchment, mock_post):
    runner.invoke(app, ["tide", "dev", "outlet", "--cron", "15 2 * * *"])
    payload = mock_post.call_args.kwargs["json"]
    assert payload["cron"] == "15 2 * * *"
    assert payload["local"] is False


def test_tide_local_flag(runner, dev_catchment, mock_post):
    runner.invoke(app, ["tide", "dev", "outlet", "--cron", "0 8 * * 1", "--local"])
    payload = mock_post.call_args.kwargs["json"]
    assert payload["local"] is True


def test_tide_requires_cron(runner, dev_catchment, mock_post):
    result = runner.invoke(app, ["tide", "dev", "outlet"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0


def test_tide_unknown_catchment_exits(runner, mock_post):
    result = runner.invoke(app, ["tide", "nonexistent", "outlet", "--cron", "* * * * *"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0
