"""Runtime (Driver) tests for the no-change skip: a Source reporting ``changed=False`` (a Duck pass
via ``pond.skip()`` / an empty Trickle delta) holds its ``changed_f``, so its Sink is completed
in-engine as a pass — no Duck dispatched. See plans/no-change-skip.md.
"""

from __future__ import annotations

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register

pytestmark = pytest.mark.timeout(5)

_R = [{"func": "f", "name": "r", "parents": []}]


def _cfg(sources=None):
    return {"sources": sources or {}, "immediate_retries": 0, "source_retries": 0, "windows": []}


def _chain(tmp_path, snk_always_run=False):
    """src (inlet) -> snk (one ripple each)."""
    db = connect(tmp_path / "duck.db")
    migrate(db)
    snk_r = [{"func": "f", "name": "r", "parents": [], "always_run": snk_always_run}]
    _register(db, "src", "1.0.0", "inlet", "ponds/src/1.0.0", {**_cfg(), "kind": "inlet"}, _R)
    _register(db, "snk", "1.0.0", "pond", "ponds/snk/1.0.0",
              {**_cfg(sources={"src": "1.0.0"}), "kind": "pond"}, snk_r)
    return Driver(db, tmp_path, "http://x", NoopLauncher())


def _run_pond(d, key, changed=True):
    """Simulate the Duck completing the pond's currently-started Run with the given change flag."""
    f = d.state.pond_states[key].start_f.isoformat()
    d.on_event(key, {"kind": "ripple", "f": f, "ripple": "r", "status": "success", "changed": changed})
    d.on_event(key, {"kind": "run_completed", "f": f, "changed": changed})
    return f


def _pond_run_changed(d, key, f):
    meta = d.meta[key]
    row = d.db.execute(
        "SELECT changed FROM pond_run WHERE pond_version_id = ? AND f = ?", (meta["version_id"], f)
    ).fetchone()
    return None if row is None else row[0]


def test_unchanged_source_makes_sink_pass_without_dispatch(tmp_path):
    d = _chain(tmp_path)
    # Cold start: tap the sink, run the source then the sink (a real bootstrap run for both).
    d.tap("snk@1")
    _run_pond(d, "src@1", changed=True)
    f_snk1 = _run_pond(d, "snk@1", changed=True)
    snk = d.state.pond_states["snk@1"]
    assert snk.changed_f.isoformat() == f_snk1  # bootstrap changed its output

    # Second cycle: re-tap, the source runs again but reports NO change.
    d.tap("snk@1")
    d.jobs["snk@1"] = []  # clear any prior queued jobs so we can see whether snk gets dispatched
    f_src2 = _run_pond(d, "src@1", changed=False)

    snk = d.state.pond_states["snk@1"]
    # The source's freshness advanced but its content mark held.
    assert d.state.pond_states["src@1"].changed_f.isoformat() != f_src2
    # The sink passed: freshness advanced to the source's, content mark held, no Duck job dispatched.
    assert snk.end_f == d.state.pond_states["src@1"].end_f
    assert snk.changed_f.isoformat() == f_snk1  # held — no real work
    assert not any(j.get("kind") == "begin_run" for j in d.jobs.get("snk@1", []))
    # The pass is recorded as a no-change pond_run (history honest).
    assert _pond_run_changed(d, "snk@1", snk.end_f.isoformat()) == 0


def test_changed_source_dispatches_sink(tmp_path):
    d = _chain(tmp_path)
    d.tap("snk@1")
    _run_pond(d, "src@1", changed=True)
    _run_pond(d, "snk@1", changed=True)

    d.tap("snk@1")
    d.jobs["snk@1"] = []
    _run_pond(d, "src@1", changed=True)  # source changed this time
    # The sink is dispatched (a real run), not passed.
    assert any(j.get("kind") == "begin_run" for j in d.jobs.get("snk@1", []))


def test_always_run_sink_dispatches_with_sources_changed_false(tmp_path):
    d = _chain(tmp_path, snk_always_run=True)
    assert d.state.ponds["snk@1"].always_run  # ORed up from the ripple
    d.tap("snk@1")
    _run_pond(d, "src@1", changed=True)
    _run_pond(d, "snk@1", changed=True)

    # Source republishes unchanged: an always_run sink still runs, and the job tells it sources_changed=False
    # so the ripple can skip its data work (pond.sources_changed() -> pond.skip()).
    d.tap("snk@1")
    d.jobs["snk@1"] = []
    _run_pond(d, "src@1", changed=False)
    jobs = [j for j in d.jobs.get("snk@1", []) if j.get("kind") == "begin_run"]
    assert jobs, "always_run sink must be dispatched even when sources are unchanged"
    assert jobs[-1]["sources_changed"] is False
