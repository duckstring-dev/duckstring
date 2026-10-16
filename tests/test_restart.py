"""A restarted Catchment must restore state from SQLite (gen, freshness, demand, ripple state) and
resume Pond Runs that were in flight when it stopped — not come up as if fresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    d.refresh_now("p@1")
    _complete_run(d)
    st = _pond(d)
    assert st["changed_f"] == st["end_f"]  # a real run advanced content freshness to its freshness

    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    assert _pond(d2)["changed_f"] == st["changed_f"], "content freshness lost on restart"


def test_restart_restores_gen_and_freshness(tmp_path):
    d = _driver(tmp_path)
    d.refresh_now("p@1")
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
    d.refresh_now("p@1")
    # Only r1 completes; r2 left in flight (Catchment "crashes" mid-run).
    f = d.state.pond_states["p@1"].start_f.isoformat()
    d.on_event("p@1", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    assert d.db.execute("SELECT status FROM pond_run").fetchone()[0] == "running"

    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    d2.resume_incomplete()
    # The incomplete run is re-dispatched to the (would-be) Duck as a begin_run job.
    assert any(j["kind"] == "begin_run" and j["f"] == f for j in d2.jobs.get("p@1", []))


def _complete(driver, key):
    """Complete the single-Ripple Pond Run in flight on ``key`` via simulated Duck events."""
    f = driver.state.pond_states[key].start_f.isoformat()
    driver.on_event(key, {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    driver.on_event(key, {"kind": "run_completed", "f": f})


def test_restart_resends_demand_the_pulse_throttle_held(tmp_path):
    """Demand the throttle holds is persisted as plain demand, but the soft state that would release
    it is lost on restart. The reload re-sends each Pond's newest unmet target once, so it reaches the Inlet
    instead of stranding behind the bottleneck (plans/pulse-throttle.md)."""
    one = [{"func": "f1", "name": "r1", "parents": []}]
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "inlet", "1.0.0", "inlet", "ponds/inlet/1.0.0", _CFG, one)
    _register(db, "slow", "1.0.0", "pond", "ponds/slow/1.0.0", {**_CFG, "kind": "pond", "sources": {"inlet": "1.0.0"}},
              one)
    _register(db, "out", "1.0.0", "outlet", "ponds/out/1.0.0", {**_CFG, "kind": "outlet", "sources": {"slow": "1.0.0"}},
              one)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    now = datetime.now(timezone.utc)
    t1, t2, t3, t4 = (now - timedelta(seconds=n) for n in (40, 30, 20, 10))
    d.refresh_now("out@1", at=t1)
    _complete(d, "inlet@1")  # slow starts at t1
    d.refresh_now("out@1", at=t2)
    _complete(d, "inlet@1")
    d.refresh_now("out@1", at=t3)  # two demands in slow's run: it holds this one
    d.refresh_now("out@1", at=t4)  # and the throttled Outlet holds this one
    assert d.state.pond_states["inlet@1"].demand == []
    assert d.state.pond_states["out@1"].throttle.held == 1

    runs = d.state.pond_states["inlet@1"].runs_started
    d2 = Driver(d.db, tmp_path, "http://x", NoopLauncher())
    th = d2.state.pond_states["out@1"].throttle
    assert th is None or th.held == 0  # the throttle state is soft
    d2._process(datetime.now(timezone.utc))
    inlet = d2.state.pond_states["inlet@1"]
    assert inlet.runs_started == runs + 1 and inlet.start_f == t4  # one Run serves the newest held demand
    assert inlet.demand == []
    d2.reload()  # a later reload adds nothing: the Run in flight covers it
    assert d2.state.pond_states["inlet@1"].demand == []


# ─── start / stop demand controls ───────────────────────────────────────────────


def test_wake_injects_run(tmp_path):
    d = _driver(tmp_path)
    d.wake("p@1")
    # One direct Pond Run is dispatched, with no upstream propagation (p is an Inlet anyway).
    assert any(j["kind"] == "begin_run" for j in d.jobs.get("p@1", []))
    assert d.db.execute("SELECT COUNT(*) FROM pond_run").fetchone()[0] == 1
    assert d.state.pond_states["p@1"].runs_started == 1


def test_sleep_clears_demand_keeps_ripple_demand(tmp_path):
    d = _driver(tmp_path)
    far = datetime(2030, 1, 1, tzinfo=timezone.utc)
    d.state.pond_states["p@1"].demand = [far]
    d.state.ripple_states["p@1.r2"].demand = [far]  # an in-flight run
    d.sleep("p@1")
    assert not d.state.pond_states["p@1"].demand
    assert d.state.ripple_states["p@1.r2"].demand == [far]   # kept so the run completes
