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


# ── refresh ───────────────────────────────────────────────────────────────────


def test_refresh_now_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["refresh", "outlet", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Refresh sent" in result.output


def test_refresh_now_explicit_catchment_and_major(runner, live_catchment):
    _deploy_outlet(live_catchment)
    for extra in (["-c", "dev"], ["--major", "1"]):
        result = runner.invoke(app, ["refresh", "outlet", *extra, "--silent"])
        assert result.exit_code == 0, result.output


def test_refresh_unknown_catchment_or_no_default_exits(runner):
    assert runner.invoke(app, ["refresh", "outlet", "-c", "nonexistent"]).exit_code != 0
    assert runner.invoke(app, ["refresh", "outlet"]).exit_code != 0


def test_refresh_add_ls_rm(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["refresh", "add", "outlet", "--every", "30m", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Every 30 min" in result.output
    result = runner.invoke(app, ["refresh", "add", "outlet", "--every", "1d", "--start", "06:00",
                                 "--on", "MON,TUE,WED,THU,FRI", "--silent"])
    assert "Weekdays at 06:00" in result.output, result.output
    result = runner.invoke(app, ["refresh", "add", "outlet", "--continuous", "--silent"])  # replaces Every 30 min
    assert "Continuous" in result.output, result.output

    result = runner.invoke(app, ["refresh", "ls", "outlet"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "Weekdays at 06:00" in result.output and "Continuous" in result.output
    assert "Every 30 min" not in result.output

    result = runner.invoke(app, ["refresh", "rm", "outlet", "Continuous"])
    assert result.exit_code == 0, result.output
    assert "Continuous" not in runner.invoke(app, ["refresh", "ls", "outlet"]).output


def test_refresh_add_validates(runner, live_catchment):
    for args in (["outlet"], ["outlet", "--every", "soon"], ["outlet", "--continuous", "--start", "06:00"],
                 ["outlet", "--every", "1d", "--on", "MON"], ["outlet", "--every", "1d", "--start", "06:00",
                                                              "--on", "FUNDAY"]):
        result = runner.invoke(app, ["refresh", "add", *args, "--silent"])
        assert result.exit_code != 0, args


def test_the_old_trigger_commands_are_gone(runner):
    for args in (["trigger", "pulse", "outlet"], ["control", "refresh", "outlet"]):
        assert runner.invoke(app, args).exit_code != 0, args


# ── start / stop ─────────────────────────────────────────────────────────────────


def test_wake_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["control", "wake", "outlet", "--silent"])
    assert result.exit_code == 0, result.output
    assert "Woken" in result.output


def test_sleep_succeeds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["control", "sleep", "outlet"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Asleep."


def test_sleep_upstream(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["control", "sleep", "outlet", "--upstream"])
    assert result.exit_code == 0, result.output
    assert "upstream" in result.output


def test_sleep_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["control", "sleep", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_refresh_add_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["refresh", "add", "outlet", "--every", "60s", "-c", "nonexistent"])
    assert result.exit_code != 0
