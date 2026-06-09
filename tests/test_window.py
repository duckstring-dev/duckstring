"""Tests for batch-availability windows: the engine recurrence math, the driver CRUD + overlap
validation, the CLI parsers, and the CLI end-to-end against a live Catchment.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import typer

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register
from duckstring.cli import app
from duckstring.cli.window import _parse_days, _parse_dt, _parse_duration, _parse_every
from duckstring.engine import Window
from duckstring.engine.core import _DAYS

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def secs(x: float) -> timedelta:
    return timedelta(seconds=x)


# ─── Engine: recurrence math ─────────────────────────────────────────────────────


@pytest.mark.timeout(1)
def test_window_active_end_recurs():
    w = Window(start_anchor=T0, duration=secs(20), freq_unit="MINUTE", freq_interval=1)
    assert w.active_end(T0 + secs(5)) == T0 + secs(20)   # inside first window
    assert w.active_end(T0 + secs(30)) is None           # in the gap
    assert w.active_end(T0 + secs(65)) == T0 + secs(80)  # inside the second window [60, 80)


@pytest.mark.timeout(1)
def test_window_valid_days_filter():
    today = _DAYS[T0.weekday()]
    w = Window(start_anchor=T0, duration=secs(3600), freq_unit="DAY", freq_interval=1,
               valid_days=frozenset({today}))
    assert w.active_end(T0 + secs(60)) == T0 + secs(3600)          # today is allowed
    assert w.active_end(T0 + timedelta(days=1) + secs(60)) is None  # next day not allowed
    assert w.active_end(T0 + timedelta(days=7) + secs(60)) is not None  # a week later: same weekday


@pytest.mark.timeout(1)
def test_window_until_expires():
    w = Window(start_anchor=T0, duration=secs(20), freq_unit="MINUTE", freq_interval=1, until=T0 + secs(90))
    assert w.active_end(T0 + secs(65)) is not None   # window at +60 is within until
    assert w.active_end(T0 + secs(125)) is None       # window at +120 is past until


@pytest.mark.timeout(1)
def test_window_next_boundary():
    w = Window(start_anchor=T0, duration=secs(20), freq_unit="MINUTE", freq_interval=1)
    assert w.next_boundary(T0 + secs(5)) == T0 + secs(20)   # close of the active window
    assert w.next_boundary(T0 + secs(30)) == T0 + secs(60)  # next open from the gap


# ─── Driver: CRUD + overlap ──────────────────────────────────────────────────────

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"}


def _driver(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "tx", "1.0.0", "inlet", "ponds/tx/1.0.0", _CFG, [{"func": "f", "name": "ingest", "parents": []}])
    return Driver(db, tmp_path, "http://x", NoopLauncher())


@pytest.mark.timeout(2)
def test_driver_window_add_list_remove(tmp_path):
    d = _driver(tmp_path)
    d.add_window("tx", "nightly", "2026-01-01T02:00:00+00:00", 3600, "DAY", 1)
    ws = d.list_windows("tx")
    assert len(ws) == 1 and ws[0]["name"] == "nightly" and ws[0]["freq_unit"] == "DAY"
    assert len(d.state.ponds["tx"].windows) == 1  # loaded into the engine
    assert d.remove_window("tx", "nightly") is True
    assert d.list_windows("tx") == []
    assert d.state.ponds["tx"].windows == []
    assert d.remove_window("tx", "nightly") is False  # already gone


@pytest.mark.timeout(2)
def test_driver_window_duplicate_name(tmp_path):
    d = _driver(tmp_path)
    d.add_window("tx", "w", "2026-01-01T00:00:00+00:00", 600, "HOUR", 1)
    with pytest.raises(ValueError, match="already exists"):
        d.add_window("tx", "w", "2026-01-01T05:00:00+00:00", 600, "HOUR", 1)


@pytest.mark.timeout(2)
def test_driver_window_overlap_rejected(tmp_path):
    d = _driver(tmp_path)
    # 'a': 1h windows every 2h → [00:00,01:00), [02:00,03:00), ...
    d.add_window("tx", "a", "2026-01-01T00:00:00+00:00", 3600, "HOUR", 2)
    # 'b' at 00:30 collides with a's [00:00,01:00).
    with pytest.raises(ValueError, match="overlaps"):
        d.add_window("tx", "b", "2026-01-01T00:30:00+00:00", 600, "HOUR", 2)
    # 'c' at 01:30 sits in the gap → accepted.
    d.add_window("tx", "c", "2026-01-01T01:30:00+00:00", 600, "HOUR", 2)
    assert {w["name"] for w in d.list_windows("tx")} == {"a", "c"}


# ─── CLI parsers ─────────────────────────────────────────────────────────────────


@pytest.mark.timeout(1)
def test_parse_duration():
    assert _parse_duration("3h") == 10800
    assert _parse_duration("45m") == 2700
    assert _parse_duration("1h30m") == 5400
    assert _parse_duration("15s") == 15
    with pytest.raises(typer.BadParameter):
        _parse_duration("3x")


@pytest.mark.timeout(1)
def test_parse_every():
    assert _parse_every("1d") == ("DAY", 1)
    assert _parse_every("10s") == ("SECOND", 10)
    assert _parse_every("12h") == ("HOUR", 12)
    with pytest.raises(typer.BadParameter):
        _parse_every("1h30m")  # combined not allowed for --every


@pytest.mark.timeout(1)
def test_parse_days_and_dt():
    assert _parse_days("mon,wed,fri") == "MON,WED,FRI"
    assert _parse_days(None) is None
    with pytest.raises(typer.BadParameter):
        _parse_days("xyz")
    assert _parse_dt("2026-06-08T14:00:00", allow_hhmm=True).startswith("2026-06-08T14:00:00")
    assert "T02:00:00" in _parse_dt("02:00", allow_hhmm=True)
    with pytest.raises(typer.BadParameter):
        _parse_dt("02:00", allow_hhmm=False)


# ─── CLI end-to-end ──────────────────────────────────────────────────────────────


def _deploy_inlet(url: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", '[pond]\nname = "tx"\nversion = "1.0.0"\ntype = "inlet"\n')
    httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
        data={"name": "tx", "version": "1.0.0", "type": "inlet"},
        timeout=10.0,
    )


@pytest.mark.timeout(30)
def test_window_cli_roundtrip(runner, live_catchment):
    _deploy_inlet(live_catchment)
    r = runner.invoke(app, ["trigger", "window", "tx", "add", "-n", "nightly", "-s", "02:00", "-d", "3h", "-e", "1d"])
    assert r.exit_code == 0, r.output
    assert "Window 'nightly' added" in r.output

    r = runner.invoke(app, ["trigger", "window", "tx", "list"])
    assert r.exit_code == 0, r.output
    assert "nightly" in r.output and "3h" in r.output

    r = runner.invoke(app, ["trigger", "window", "tx", "remove", "nightly"])
    assert r.exit_code == 0, r.output
    assert "removed" in r.output

    r = runner.invoke(app, ["trigger", "window", "tx", "list"])
    assert "No windows" in r.output


@pytest.mark.timeout(30)
def test_window_cli_defaults(runner, live_catchment):
    # Only --name and --every required: --start defaults to 00:00 today, --duration to --every.
    _deploy_inlet(live_catchment)
    r = runner.invoke(app, ["trigger", "window", "tx", "add", "-n", "back2back", "-e", "1h"])
    assert r.exit_code == 0, r.output

    windows = httpx.get(f"{live_catchment}/api/ponds/tx/windows").json()["windows"]
    assert len(windows) == 1
    w = windows[0]
    assert w["duration_seconds"] == 3600        # defaulted to the --every (1h) interval
    assert w["freq_unit"] == "HOUR" and w["freq_interval"] == 1
    assert w["start_anchor"].endswith("T00:00:00+00:00")  # midnight today UTC
