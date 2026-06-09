from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from duckstring.cli import app

pytestmark = pytest.mark.timeout(30)

_OUTLET_TOML = b"[pond]\nname = \"outlet\"\nversion = \"1.0.0\"\ntype = \"outlet\"\n"


def _deploy_outlet(url: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", _OUTLET_TOML)
    httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
        data={"name": "outlet", "version": "1.0.0", "type": "outlet"},
        timeout=10.0,
    )


# ── pulse ─────────────────────────────────────────────────────────────────────


def test_pulse_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Pulse sent" in result.output


def test_pulse_explicit_catchment(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "-c", "dev", "--silent"])
    assert result.exit_code == 0, result.output


def test_pulse_with_major(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "--major", "1", "--silent"])
    assert result.exit_code == 0, result.output


def test_tap_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "tap", "outlet", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Tap sent" in result.output


def test_pulse_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_pulse_no_default_exits(runner):
    result = runner.invoke(app, ["trigger", "pulse", "outlet"])
    assert result.exit_code != 0


# ── wave ──────────────────────────────────────────────────────────────────────


def test_wave_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "wave", "outlet", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Wave started" in result.output


def test_wave_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "wave", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


# ── tide ──────────────────────────────────────────────────────────────────────


def test_tide_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "tide", "outlet", "1h", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Tide started" in result.output


def test_tide_requires_bound(runner, live_catchment):
    result = runner.invoke(app, ["trigger", "tide", "outlet"])
    assert result.exit_code != 0


def test_tide_rejects_bad_bound(runner, live_catchment):
    result = runner.invoke(app, ["trigger", "tide", "outlet", "soon"])
    assert result.exit_code != 0


# ── remove ──────────────────────────────────────────────────────────────────────


def test_remove_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    runner.invoke(app, ["trigger", "wave", "outlet", "--silent"])
    result = runner.invoke(app, ["trigger", "remove", "outlet"])
    assert result.exit_code == 0, result.output
    assert "Trigger removed" in result.output


def test_remove_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "remove", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


# ── start / stop ─────────────────────────────────────────────────────────────────


def test_start_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "start", "outlet", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Started" in result.output


def test_stop_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "stop", "outlet"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Stopped."


def test_stop_upstream(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "stop", "outlet", "--upstream"])
    assert result.exit_code == 0, result.output
    assert "upstream" in result.output


def test_stop_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "stop", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_tide_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "tide", "outlet", "60s", "-c", "nonexistent"])
    assert result.exit_code != 0
