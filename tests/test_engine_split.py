"""Tests for the engine split: the Catchment brain emits the right Pond Run commands, the push-only
Duck engine completes runs, and the run ledger persists/reconciles. The full composed behaviour stays
covered by test_engine.py; here we exercise the two halves the runtime actually deploys.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from duckstring.engine import (
    BeginRun,
    EngineState,
    Pond,
    PondState,
    Ripple,
    RippleState,
    complete_ripple,
    drain_begin_runs,
    pulse_pond,
    sentinel,
    tap_pond,
    worker,
)
from duckstring.engine import pond as ledger

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(milliseconds=100)


def secs(x: float) -> timedelta:
    return timedelta(seconds=x)


# ─── Catchment brain: BeginRun emission ───────────────────────────────────────


def chain_state() -> EngineState:
    """p1 (inlet: r1, r2 -> r3) -> p2 (single ripple s1)."""
    ponds = [Pond("p1", "p1"), Pond("p2", "p2", sources=["p1"])]
    ripples = [
        Ripple("r1", "p1", "r1"),
        Ripple("r2", "p1", "r2"),
        Ripple("r3", "p1", "r3", parents=["r1", "r2"]),
        Ripple("s1", "p2", "s1"),
    ]
    return EngineState(
        ponds={p.id: p for p in ponds},
        pond_states={p.id: PondState() for p in ponds},
        ripples={r.id: r for r in ripples},
        ripple_states={r.id: RippleState() for r in ripples},
    )


class CatchmentSim:
    """Drives the Catchment engine while a fake set of Ducks executes ripples after fixed durations.
    Collects the BeginRun commands the Catchment would dispatch."""

    def __init__(self, state: EngineState, durations: dict[str, timedelta]):
        self.state = state
        self.durations = durations
        self.now = T0
        self.inflight: dict[str, datetime] = {}
        self.begin_runs: list[BeginRun] = []

    def _react(self) -> None:
        self.state, started = sentinel(self.now, self.state)
        self.begin_runs += drain_begin_runs(self.state)
        for rid in started:
            self.inflight[rid] = self.now + self.durations[rid]

    def trigger(self, fn, pid: str) -> None:
        self.state = fn(self.state, pid, self.now)
        self._react()

    def run(self, seconds: float) -> None:
        for _ in range(int(seconds / 0.1)):
            for rid in [r for r, t in self.inflight.items() if t <= self.now]:
                self.state = complete_ripple(self.state, rid, self.now)
                del self.inflight[rid]
            self._react()
            self.now += STEP


@pytest.mark.timeout(5)
def test_catchment_emits_begin_run_per_pond_run():
    # A Tap should make p1 run 3 Pond Runs (its ripple depth) and p2 one — emitted as BeginRuns.
    sim = CatchmentSim(chain_state(), {r: secs(1) for r in ("r1", "r2", "r3", "s1")})
    sim.trigger(tap_pond, "p2")
    sim.run(20)
    p1_runs = [b for b in sim.begin_runs if b.pond_id == "p1"]
    p2_runs = [b for b in sim.begin_runs if b.pond_id == "p2"]
    assert len(p1_runs) == 3
    assert len(p2_runs) == 1
    # Each BeginRun freshness matches the pond's recorded run starts.
    assert sim.state.pond_states["p1"].runs_completed == 3


@pytest.mark.timeout(5)
def test_catchment_pulse_one_begin_run_each():
    sim = CatchmentSim(chain_state(), {r: secs(1) for r in ("r1", "r2", "r3", "s1")})
    sim.trigger(pulse_pond, "p2")
    sim.run(20)
    assert len([b for b in sim.begin_runs if b.pond_id == "p1"]) == 1
    assert len([b for b in sim.begin_runs if b.pond_id == "p2"]) == 1


# ─── Duck push engine ─────────────────────────────────────────────────────────


def sales_parents() -> dict[str, list[str]]:
    return {"daily": [], "tiers": [], "join": ["daily", "tiers"]}


@pytest.mark.timeout(5)
def test_worker_pushes_run_to_completion():
    s = worker.new_state(sales_parents())
    f = T0
    s = worker.begin_run(s, f)
    now = T0
    completed = None
    inflight: dict[str, datetime] = {}
    durs = {"daily": secs(2), "tiers": secs(1), "join": secs(3)}
    for _ in range(200):
        for name in [n for n, t in inflight.items() if t <= now]:
            s, rc = worker.complete_ripple(s, name, now)
            del inflight[name]
            if rc:
                completed = rc
        s, launched = worker.sentinel(now, s)
        for name in launched:
            inflight[name] = now + durs[name]
        if completed:
            break
        now += STEP
    assert completed is not None and completed.f == f
    assert s.states["join"].end_f == f


@pytest.mark.timeout(5)
def test_worker_pipelines_two_runs():
    # Two Pond Runs in flight: roots run ahead of the leaf through the bottleneck.
    s = worker.new_state(sales_parents())
    s = worker.begin_run(s, T0)
    s = worker.begin_run(s, T0 + secs(1))
    # daily/tiers each have two targets queued (one per run).
    assert len(s.states["daily"].targets) == 2
    assert len(s.states["join"].targets) == 2


def _run_to_join(s: worker.WorkerState) -> worker.WorkerState:
    """Advance the sales topology until ``join`` is the running Ripple."""
    s, _ = worker.sentinel(T0, s)  # daily, tiers
    s, _ = worker.complete_ripple(s, "daily", T0)
    s, _ = worker.complete_ripple(s, "tiers", T0)
    s, launched = worker.sentinel(T0, s)
    assert launched == ["join"]
    return s


@pytest.mark.timeout(5)
def test_worker_immediate_retry_then_succeeds():
    # Budget 1: join errors once, is retried in the same Run, then completes.
    s = worker.new_state(sales_parents(), retry_immediately=1)
    s = worker.begin_run(s, T0)
    s = _run_to_join(s)
    s, rf = worker.fail_ripple(s, "join", T0)
    assert rf is None  # retried, not a Run failure
    assert s.immediate_left[T0] == 0  # budget spent
    s, launched = worker.sentinel(T0, s)
    assert launched == ["join"]  # relaunched
    s, rc = worker.complete_ripple(s, "join", T0)
    assert rc is not None and rc.f == T0


@pytest.mark.timeout(5)
def test_worker_immediate_budget_exhausted_fails_run():
    # Budget 0: the first error gives up the whole Pond Run, at the Ripple's freshness.
    s = worker.new_state(sales_parents(), retry_immediately=0)
    s = worker.begin_run(s, T0)
    s = _run_to_join(s)
    s, rf = worker.fail_ripple(s, "join", T0)
    assert rf == worker.RunFailed(T0, "join")
    assert not s.states["join"].is_running


# ─── Ledger ───────────────────────────────────────────────────────────────────


@pytest.mark.timeout(1)
def test_ledger_roundtrip_and_recovery(tmp_path):
    con = ledger.connect(tmp_path / "pond.db")
    f = T0
    ledger.record_pond_run_start(con, f, T0)
    ledger.record_ripple_start(con, "daily", f)
    ledger.record_ripple_complete(con, "daily", f)
    ledger.record_ripple_start(con, "tiers", f)
    ledger.record_ripple_complete(con, "tiers", f)
    # join never ran → it is the only incomplete ripple for F.
    assert ledger.incomplete_ripples(con, f, ["daily", "tiers", "join"]) == ["join"]

    # Rebuild the worker from the ledger: begin_run(F) must NOT re-stamp completed ripples.
    s = ledger.load_state(con, sales_parents())
    assert s.states["daily"].end_f == f
    s = worker.begin_run(s, f)
    assert s.states["daily"].targets == []  # already complete for F → not re-run
    assert s.states["tiers"].targets == []
    assert s.states["join"].targets == [f]  # incomplete → will re-run
