"""A restarted Catchment must restore state from SQLite (gen, freshness, demand, ripple state) and
resume Pond Runs that were in flight when it stopped — not come up as if fresh.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register

pytestmark = pytest.mark.timeout(5)

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "windows": [], "kind": "inlet"}
_RIPPLES = [{"func": "f1", "name": "r1", "parents": []}, {"func": "f2", "name": "r2", "parents": ["f1"]}]


def _make_db(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "p", "1.0.0", "inlet", "ponds/p/1.0.0", _CFG, _RIPPLES)
    return db


def _driver(tmp_path):
    return Driver(_make_db(tmp_path), tmp_path, "http://x", NoopLauncher())


def _pond(driver, name="p"):
    return next(p for p in driver.status()["ponds"] if p["name"] == name)


def _complete_run(driver):
    """Drive a full Pond Run to completion via simulated Duck events (r1 then r2)."""
    f = driver.state.pond_states["p@1"].start_f.isoformat()
    driver.on_event("p@1", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    driver.on_event("p@1", {"kind": "ripple", "f": f, "ripple": "r2", "status": "success"})
    driver.on_event("p@1", {"kind": "run_completed", "f": f})
    return f


def test_restart_restores_changed_f(tmp_path):
    d = _driver(tmp_path)
    d.pulse("p@1")
    _complete_run(d)
    st = _pond(d)
    assert st["changed_f"] == st["end_f"]  # a real run advanced content freshness to its freshness

    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    assert _pond(d2)["changed_f"] == st["changed_f"], "content freshness lost on restart"


def test_restart_restores_gen_and_freshness(tmp_path):
    d = _driver(tmp_path)
    d.pulse("p@1")
    f = _complete_run(d)
    assert _pond(d)["gen"] == 1
    assert _pond(d)["end_f"] is not None

    # Restart: a fresh Driver on the same SQLite file.
    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    st = _pond(d2)
    assert st["gen"] == 1, "generation count lost on restart"
    assert st["end_f"] is not None, "freshness lost on restart"
    # Ripple-level state restored too (engine coherence).
    assert d2.state.ripple_states["p@1.r2"].end_f.isoformat() == f


def test_restart_resumes_incomplete_run(tmp_path):
    d = _driver(tmp_path)
    d.pulse("p@1")
    # Only r1 completes; r2 left in flight (Catchment "crashes" mid-run).
    f = d.state.pond_states["p@1"].start_f.isoformat()
    d.on_event("p@1", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    assert d.db.execute("SELECT status FROM pond_run").fetchone()[0] == "running"

    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    d2.resume_incomplete()
    # The incomplete run is re-dispatched to the (would-be) Duck as a begin_run job.
    assert any(j["kind"] == "begin_run" and j["f"] == f for j in d2.jobs.get("p@1", []))


# ─── start / stop demand controls ───────────────────────────────────────────────


def test_wake_injects_run(tmp_path):
    d = _driver(tmp_path)
    d.wake("p@1")
    # One direct Pond Run is dispatched, with no upstream propagation (p is an Inlet anyway).
    assert any(j["kind"] == "begin_run" for j in d.jobs.get("p@1", []))
    assert d.db.execute("SELECT COUNT(*) FROM pond_run").fetchone()[0] == 1
    assert d.state.pond_states["p@1"].runs_started == 1


def test_sleep_clears_demand_keeps_ripple_push(tmp_path):
    d = _driver(tmp_path)
    far = datetime(2030, 1, 1, tzinfo=timezone.utc)
    d.state.pond_states["p@1"].has_pull = True
    d.state.ripple_states["p@1.r2"].has_pull = True
    d.state.ripple_states["p@1.r2"].targets = [far]  # in-flight push
    d.sleep("p@1")
    ps = d.state.pond_states["p@1"]
    assert not ps.has_pull and not ps.targets
    assert not d.state.ripple_states["p@1.r2"].has_pull       # pull cleared
    assert d.state.ripple_states["p@1.r2"].targets == [far]   # push kept so the run completes
