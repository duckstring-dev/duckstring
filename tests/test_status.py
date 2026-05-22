from __future__ import annotations

import io
import zipfile

import httpx

from duckstring.cli import app


def _deploy(url: str, *, name: str, version: str, kind: str):
    toml = f'[pond]\nname = "{name}"\nversion = "{version}"\n'
    if kind != "pond":
        toml += f'type = "{kind}"\n'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", toml)
    httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
        data={"name": name, "version": version, "type": kind},
    )


def test_status_empty_ponds_message(runner, live_catchment):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No active" in result.output


def test_status_explicit_catchment(runner, live_catchment):
    result = runner.invoke(app, ["status", "-c", "dev"])
    assert result.exit_code == 0


def test_status_renders_pond_table(runner, live_catchment):
    _deploy(live_catchment, name="outlet", version="1.0.0", kind="outlet")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "outlet" in result.output
    assert "1.0.0" in result.output


def test_status_default_shows_active_only(runner, live_catchment):
    _deploy(live_catchment, name="inlet", version="1.0.0", kind="inlet")
    _deploy(live_catchment, name="inlet", version="1.1.0", kind="inlet")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "1.1.0" in result.output
    assert "1.0.0" not in result.output


def test_status_all_shows_inactive(runner, live_catchment):
    _deploy(live_catchment, name="inlet", version="1.0.0", kind="inlet")
    _deploy(live_catchment, name="inlet", version="1.1.0", kind="inlet")
    result = runner.invoke(app, ["status", "--all"])
    assert result.exit_code == 0
    assert "1.0.0" in result.output
    assert "1.1.0" in result.output


def test_status_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["status", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_status_no_default_exits(runner):
    result = runner.invoke(app, ["status"])
    assert result.exit_code != 0
