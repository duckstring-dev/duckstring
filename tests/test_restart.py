"""A restarted Catchment must restore state from SQLite (gen, freshness, demand, ripple state) and
resume Pond Runs that were in flight when it stopped — not come up as if fresh.
"""

from __future__ import annotations

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
    f = driver.state.pond_states["p"].start_f.isoformat()
    driver.on_event("p", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    driver.on_event("p", {"kind": "ripple", "f": f, "ripple": "r2", "status": "success"})
    driver.on_event("p", {"kind": "run_completed", "f": f})
    return f


def test_restart_restores_gen_and_freshness(tmp_path):
    d = _driver(tmp_path)
    d.pulse("p")
    f = _complete_run(d)
    assert _pond(d)["gen"] == 1
    assert _pond(d)["end_f"] is not None

    # Restart: a fresh Driver on the same SQLite file.
    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    st = _pond(d2)
    assert st["gen"] == 1, "generation count lost on restart"
    assert st["end_f"] is not None, "freshness lost on restart"
    # Ripple-level state restored too (engine coherence).
    assert d2.state.ripple_states["p.r2"].end_f.isoformat() == f


def test_restart_resumes_incomplete_run(tmp_path):
    d = _driver(tmp_path)
    d.pulse("p")
    # Only r1 completes; r2 left in flight (Catchment "crashes" mid-run).
    f = d.state.pond_states["p"].start_f.isoformat()
    d.on_event("p", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    assert d.db.execute("SELECT status FROM pond_run").fetchone()[0] == "running"

    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    d2.resume_incomplete()
    # The incomplete run is re-dispatched to the (would-be) Duck as a begin_run job.
    assert any(j["kind"] == "begin_run" and j["f"] == f for j in d2.jobs.get("p", []))
