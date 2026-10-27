"""A Duck told to shut down while idle keeps going until it's idle with its S3 copy done (persist), so it can still
be alive when the next Run is dispatched. The launcher saw a live process and started nothing, the Run waited for a
Duck that never polled again, and when it exited the liveness check failed the Run ("Duck process is not running").
Found by the TPC-DS benchmark's S3 runs (2026-10-25): the first build's copy to S3 took long enough for the change
Run to land in the window. Now the Catchment treats a Duck that collected a shutdown as departing: if it picks up the
Run it stays, and if it exits first a fresh Duck takes the Run."""

from __future__ import annotations

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register

pytestmark = pytest.mark.timeout(5)

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "windows": [], "kind": "inlet"}
_RIPPLES = [{"func": "f1", "name": "r1", "parents": []}]


class _Ducks(NoopLauncher):
    """One pretend Duck per Pond, running until the test says it exited."""

    manages_processes = True

    def __init__(self):
        self.running: set[str] = set()
        self.spawned = 0

    def is_running(self, pond_key):
        return pond_key in self.running

    def ensure(self, pond_key, version, source_path, duck=None):
        if pond_key not in self.running:
            self.running.add(pond_key)
            self.spawned += 1

    def terminate(self, pond_key, wait=False):
        self.running.discard(pond_key)


def _driver(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "p", "1.0.0", "inlet", "ponds/p/1.0.0", _CFG, _RIPPLES)
    launcher = _Ducks()
    return Driver(db, tmp_path, "http://x", launcher), launcher


def _run_once(d):
    d.refresh_now("p@1")
    jobs = d.take_jobs("p@1", version="1.0.0")
    assert [j["kind"] for j in jobs] == ["begin_run"]
    f = jobs[0]["f"]
    d.on_event("p@1", {"kind": "ripple", "f": f, "ripple": "r1", "status": "success"})
    d.on_event("p@1", {"kind": "run_completed", "f": f, "schema": {"t": {"a": "INTEGER"}}})


def _shut_down(d):
    """The idle reap's shutdown, collected by the Duck (which then finishes its S3 copy before exiting)."""
    d._enqueue_job("p@1", {"kind": "shutdown", "retain_from": None})
    assert [j["kind"] for j in d.take_jobs("p@1", version="1.0.0")] == ["shutdown"]


def test_a_run_dispatched_while_its_duck_is_departing_goes_to_a_fresh_duck(tmp_path):
    d, launcher = _driver(tmp_path)
    _run_once(d)
    _shut_down(d)
    d.refresh_now("p@1")  # the next Run, while the departing Duck is still alive
    launcher.running.discard("p@1")  # it exits without polling again
    d.scheduler_tick()
    ps = d.state.pond_states["p@1"]
    assert not ps.is_failed
    assert launcher.spawned == 2  # a fresh Duck
    jobs = d.take_jobs("p@1", version="1.0.0")
    assert [j["kind"] for j in jobs] == ["begin_run"]  # and it gets the queued Run
    d.on_event("p@1", {"kind": "ripple", "f": jobs[0]["f"], "ripple": "r1", "status": "success"})
    d.on_event("p@1", {"kind": "run_completed", "f": jobs[0]["f"], "schema": {"t": {"a": "INTEGER"}}})
    assert d.status()["ponds"][0]["status"] == "idle"


def test_a_departing_duck_that_picks_up_the_run_keeps_it(tmp_path):
    d, launcher = _driver(tmp_path)
    _run_once(d)
    _shut_down(d)
    d.refresh_now("p@1")
    assert [j["kind"] for j in d.take_jobs("p@1", version="1.0.0")] == ["begin_run"]  # it polled in time
    d.scheduler_tick()
    assert launcher.spawned == 1  # no second Duck beside it
    assert not d.state.pond_states["p@1"].is_failed


def test_a_departed_duck_with_no_work_is_simply_forgotten(tmp_path):
    d, launcher = _driver(tmp_path)
    _run_once(d)
    _shut_down(d)
    launcher.running.discard("p@1")
    d.scheduler_tick()
    assert launcher.spawned == 1  # nothing queued: no Duck until the next Run
    _run_once(d)  # which starts one as usual
    assert launcher.spawned == 2
