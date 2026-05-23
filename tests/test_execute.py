from __future__ import annotations

import io
import zipfile

import httpx

from duckstring.cli import app

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
    result = runner.invoke(app, ["trigger", "pulse", "outlet"])
    assert result.exit_code == 0, result.output
    assert "Pulse sent" in result.output


def test_pulse_explicit_catchment(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "-c", "dev"])
    assert result.exit_code == 0, result.output


def test_pulse_with_version(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "--version", "1"])
    assert result.exit_code == 0, result.output


def test_pulse_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "pulse", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_pulse_no_default_exits(runner):
    result = runner.invoke(app, ["trigger", "pulse", "outlet"])
    assert result.exit_code != 0


# ── wave ──────────────────────────────────────────────────────────────────────


def test_wave_succeeds(runner, live_catchment):
    result = runner.invoke(app, ["trigger", "wave", "outlet"])
    assert result.exit_code == 0, result.output
    assert "Wave started" in result.output


def test_wave_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "wave", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


# ── tide ──────────────────────────────────────────────────────────────────────


def test_tide_succeeds(runner, live_catchment):
    result = runner.invoke(app, ["trigger", "tide", "outlet", "--cron", "15 2 * * *"])
    assert result.exit_code == 0, result.output
    assert "Tide scheduled" in result.output


def test_tide_local_flag(runner, live_catchment):
    result = runner.invoke(app, ["trigger", "tide", "outlet", "--cron", "0 8 * * 1", "--local"])
    assert result.exit_code == 0, result.output


def test_tide_requires_cron(runner, live_catchment):
    result = runner.invoke(app, ["trigger", "tide", "outlet"])
    assert result.exit_code != 0


def test_tide_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["trigger", "tide", "outlet", "-c", "nonexistent", "--cron", "* * * * *"])
    assert result.exit_code != 0
