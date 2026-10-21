"""A redeploy re-points a Pond at its new version while its Duck may still be finishing a Run of the old one.
That Run's events belong to the version that started it: recording them against the new version broke
``ripple_run``'s foreign key to ``pond_run``, the Duck replayed the rejected event forever, and the Pond
showed as running for good (found recording the docs, 2026-10-17)."""

from __future__ import annotations

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register

pytestmark = pytest.mark.timeout(5)

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "windows": [], "kind": "inlet"}
_RIPPLES = [{"func": "f1", "name": "r1", "parents": []}, {"func": "f2", "name": "r2", "parents": ["f1"]}]


def _runs(db):
    return db.execute(
        "SELECT pv.version, pr.status, (SELECT count(*) FROM ripple_run rr WHERE rr.pond_version_id = pr.pond_version_id "
        "AND rr.f = pr.f AND rr.status = 'success') FROM pond_run pr JOIN pond_version pv ON pv.id = pr.pond_version_id "
        "ORDER BY pr.f"
    ).fetchall()


def _finish(d, f, ripples=("r1", "r2")):
    for r in ripples:
        d.on_event("p@1", {"kind": "ripple", "f": f, "ripple": r, "status": "success"})
    d.on_event("p@1", {"kind": "run_completed", "f": f, "schema": {"t": {"a": "INTEGER"}}})


def test_a_run_in_flight_across_a_redeploy_completes_under_its_own_version(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "p", "1.0.0", "inlet", "ponds/p/1.0.0", _CFG, _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    d.refresh_now("p@1")
    f = d.state.pond_states["p@1"].start_f.isoformat()
    d.on_event("p@1", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})

    _register(db, "p", "1.0.1", "inlet", "ponds/p/1.0.1", _CFG, _RIPPLES)
    d.reload()
    d.clear_on_redeploy("p", 1)
    _finish(d, f, ripples=("r2",))  # the old Duck finishes the Run it started

    assert _runs(db) == [("1.0.0", "success", 2)]
    assert _runs(db)[0][1] == "success"
    assert d.status()["ponds"][0]["status"] == "idle"
    schema_versions = {v for (v,) in db.execute(
        "SELECT pv.version FROM pond_version_schema s JOIN pond_version pv ON pv.id = s.pond_version_id")}
    assert schema_versions == {"1.0.0"}  # the old Run's output is the old version's contract

    d.refresh_now("p@1")  # the next Run is the new version's
    f2 = d.state.pond_states["p@1"].start_f.isoformat()
    _finish(d, f2)
    assert _runs(db) == [("1.0.0", "success", 2), ("1.0.1", "success", 2)]


class _OneDuck(NoopLauncher):
    """A launcher with one pretend Duck per Pond: running until the test says it exited."""

    manages_processes = True

    def __init__(self):
        self.running: dict[str, str] = {}
        self.spawned: list[tuple[str, str]] = []

    def is_running(self, pond_key):
        return pond_key in self.running

    def ensure(self, pond_key, version, source_path, duck=None):
        if pond_key not in self.running:
            self.running[pond_key] = version
            self.spawned.append((pond_key, version))


def test_a_duck_on_a_replaced_version_hands_its_work_to_a_new_duck(tmp_path):
    """A warm Duck used to keep running the old code after a redeploy for as long as a recurring Refresh kept
    it busy, so a fix redeploy never took effect. Now the outdated Duck is told only to shut down (it finishes
    what it holds), and the queued Run waits for a Duck on the new version."""
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "p", "1.0.0", "inlet", "ponds/p/1.0.0", _CFG, _RIPPLES)
    launcher = _OneDuck()
    d = Driver(db, tmp_path, "http://x", launcher)
    d.refresh_now("p@1")
    assert [j["kind"] for j in d.take_jobs("p@1", version="1.0.0")] == ["begin_run"]
    _finish(d, d.state.pond_states["p@1"].start_f.isoformat())

    _register(db, "p", "1.0.1", "inlet", "ponds/p/1.0.1", _CFG, _RIPPLES)
    d.reload()
    d.refresh_now("p@1")
    assert d.take_jobs("p@1", version="1.0.0") == [{"kind": "shutdown"}]  # the old Duck gets no new work
    d.scheduler_tick()
    assert launcher.spawned == [("p@1", "1.0.0")]  # still finishing up: no second Duck beside it

    del launcher.running["p@1"]  # it exited
    d.scheduler_tick()
    assert launcher.spawned[-1] == ("p@1", "1.0.1")
    assert [j["kind"] for j in d.take_jobs("p@1", version="1.0.1")] == ["begin_run"]
    assert d.take_jobs("p@1") == []  # a Duck that doesn't say its version is taken at its word
