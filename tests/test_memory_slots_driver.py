"""The Driver's half of memory slots (plans/memory-budget.md): the budget and slot count from the environment,
DuckDB's limit on each ``begin_run`` job, a Ripple's declared memory, the out-of-memory retry with every slot,
and the waiting status."""

from __future__ import annotations

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register

pytestmark = pytest.mark.timeout(5)

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "windows": [], "kind": "inlet"}
OOM = "OutOfMemoryException: Out of Memory Error: could not allocate block of size 256.0 KiB"


def _driver(tmp_path, monkeypatch, slots="2", budget="8GB", ponds=(("a", None), ("b", None), ("c", None))):
    monkeypatch.setenv("DUCKSTRING_RUN_SLOTS", slots)
    monkeypatch.setenv("DUCKSTRING_MEMORY_BUDGET", budget)
    db = connect(tmp_path / "duck.db")
    migrate(db)
    for name, memory in ponds:
        _register(db, name, "1.0.0", "inlet", f"ponds/{name}/1.0.0", _CFG,
                  [{"func": "f", "name": "r", "parents": [], "memory": memory}])
    return Driver(db, tmp_path, "http://x", NoopLauncher())


def _begin(d, pond):
    return [j for j in d.jobs.get(pond, []) if j.get("kind") == "begin_run"]


def _finish(d, pond):
    f = d.state.pond_states[pond].start_f.isoformat()
    d.on_event(pond, {"kind": "ripple", "f": f, "ripple": "r", "status": "success"})
    d.on_event(pond, {"kind": "run_completed", "f": f})


def test_a_lone_run_gets_its_slots_worth_of_duckdb_memory(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch)
    d.refresh_now("a@1")
    job = _begin(d, "a@1")[0]
    assert job["memory_limit"] == "4000MB"  # one 4 GB slot: the other is kept for b or c
    assert job["idle_memory"] == "256MB"
    d.refresh_now("b@1")
    assert _begin(d, "b@1")[0]["memory_limit"] == "4000MB"  # and b, refreshed later, finds it


def test_the_only_pond_on_the_machine_gets_all_of_it(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch, ponds=(("a", None),))
    d.refresh_now("a@1")
    assert _begin(d, "a@1")[0]["memory_limit"] == "8000MB"


def test_no_slots_configured_sends_no_limit(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch, slots="0")
    d.refresh_now("a@1")
    assert _begin(d, "a@1")[0]["memory_limit"] is None
    assert d.status()["ponds"][0]["memory"] is None


def test_declared_memory_takes_slots_and_is_left_out_of_duckdbs_share(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch, slots="4", budget="16GB", ponds=(("a", 3 * 10**9), ("b", None)))
    assert d.state.ponds["a@1"].slot_need == 2  # one slot for DuckDB, one for the 3 GB outside it
    d.refresh_now("b@1")  # b takes one slot... and, alone, the spare ones, less one kept while a has demand
    d.refresh_now("a@1")
    a_job = _begin(d, "a@1")
    if a_job:  # a fit beside b
        assert a_job[0]["memory_limit"] == f"{(2 * 4 - 3) * 1000}MB"  # its 8 GB less the 3 GB outside DuckDB
    else:  # a waits for its two slots, and says so
        st = {p["id"]: p for p in d.status()["ponds"]}["a@1"]
        assert st["status"] == "waiting" and st["memory"]["need"] == 2 and st["memory"]["position"] == 1
        _finish(d, "b@1")
        assert _begin(d, "a@1")[0]["memory_limit"].endswith("MB")


def test_a_pond_waiting_for_memory_reports_it_and_its_place(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch, slots="1")
    for pond in ("a@1", "b@1", "c@1"):
        d.refresh_now(pond)
    st = {p["id"]: p for p in d.status()["ponds"]}
    assert st["a@1"]["status"] == "running" and st["a@1"]["memory"]["slots"] == 1
    assert (st["b@1"]["status"], st["b@1"]["memory"]["position"]) == ("waiting", 1)
    assert (st["c@1"]["status"], st["c@1"]["memory"]["position"]) == ("waiting", 2)
    _finish(d, "a@1")
    st = {p["id"]: p for p in d.status()["ponds"]}
    assert st["b@1"]["status"] == "running" and st["c@1"]["memory"]["position"] == 1


def test_running_out_of_memory_is_retried_once_with_every_slot(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch, slots="2")
    d.refresh_now("a@1")
    d.refresh_now("b@1")
    f = d.state.pond_states["a@1"].start_f.isoformat()
    d.jobs["a@1"] = []
    d.on_event("a@1", {"kind": "failed", "f": f, "ripple": "r", "error": OOM})
    ps = d.state.pond_states["a@1"]
    assert not ps.is_failed and not ps.is_blocked  # not a failure: a retry, outside the failure budget
    assert ps.slot_exclusive or ps.slots == 2  # it asks for every slot (and has them once b is done)
    if not _begin(d, "a@1"):
        _finish(d, "b@1")  # b drains; a gets the machine
    assert _begin(d, "a@1")[0]["memory_limit"] == "8000MB"
    status = db_status(d)
    assert status == [("failed", OOM + "; retrying with every memory slot")]

    f2 = d.state.pond_states["a@1"].start_f.isoformat()
    d.on_event("a@1", {"kind": "failed", "f": f2, "ripple": "r", "error": OOM})  # fails again, alone
    ps = d.state.pond_states["a@1"]
    assert ps.is_failed
    err = {p["id"]: p for p in d.status()["ponds"]}["a@1"]["error"]
    assert "with every memory slot on the machine to itself" in err


def db_status(d):
    return [tuple(r) for r in d.db.execute(
        "SELECT pr.status, pr.error FROM pond_run pr JOIN pond_version pv ON pv.id = pr.pond_version_id "
        "JOIN pond_name pn ON pn.id = pv.pond_name_id WHERE pn.name = 'a' AND pr.status = 'failed'").fetchall()]


def test_a_success_resets_the_memory_retry(tmp_path, monkeypatch):
    d = _driver(tmp_path, monkeypatch, slots="2")
    d.refresh_now("a@1")
    f = d.state.pond_states["a@1"].start_f.isoformat()
    d.on_event("a@1", {"kind": "failed", "f": f, "ripple": "r", "error": OOM})
    _finish(d, "a@1")  # the retry succeeds
    assert "a@1" not in d._memory_retry
    d.refresh_now("a@1")
    f = d.state.pond_states["a@1"].start_f.isoformat()
    d.on_event("a@1", {"kind": "failed", "f": f, "ripple": "r", "error": OOM})
    assert not d.state.pond_states["a@1"].is_failed  # a later memory failure gets its retry again
