"""Tests for the Duck core: it drives a Pond Run to completion, persists to the ledger, and — the
headline — keeps running and buffers events when the Catchment is offline, replaying them on
reconnect. Uses a fake executor (no real DuckDB/ripple code); real execution is covered end-to-end in
test_runtime.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from duckstring.duck.core import DuckCore
from duckstring.engine import pond as ledger

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(milliseconds=100)
SALES = {"daily": [], "tiers": [], "join": ["daily", "tiers"]}
DURS = {"daily": timedelta(seconds=2), "tiers": timedelta(seconds=1), "join": timedelta(seconds=3)}


class FakeRun:
    """Schedules ripple completions after fixed durations and feeds them back to a DuckCore, standing
    in for the executor. ``export_calls`` counts parquet exports (run completions)."""

    def __init__(self, core: DuckCore):
        self.core = core
        self.now = T0
        self.inflight: dict[str, datetime] = {}
        self.export_calls = 0

    def _export(self):
        self.export_calls += 1

    def launch(self, names):
        for n in names:
            self.inflight[n] = self.now + DURS[n]

    def begin(self, f):
        self.launch(self.core.begin_run(f, self.now))

    def run(self, seconds: float):
        for _ in range(int(seconds / 0.1)):
            for name in [n for n, t in self.inflight.items() if t <= self.now]:
                del self.inflight[name]
                self.launch(self.core.ripple_completed(name, self.now, export=self._export))
            self.now += STEP


@pytest.mark.timeout(5)
def test_duck_completes_pond_run(tmp_path):
    con = ledger.connect(tmp_path / "pond.db")
    core = DuckCore("sales", con, SALES)
    sim = FakeRun(core)
    sim.begin(T0)
    sim.run(10)
    # Run completed: leaf fresh to F, ledger reflects it, parquet exported once.
    assert core.state.states["join"].end_f == T0
    assert ledger.read_pond_end_f(con) == T0
    assert sim.export_calls == 1
    # A run_completed event was buffered for the Catchment.
    assert any(e.kind == "run_completed" and e.f == T0 for e in core.events)


@pytest.mark.timeout(5)
def test_duck_survives_offline_catchment_and_replays(tmp_path):
    con = ledger.connect(tmp_path / "pond.db")
    core = DuckCore("sales", con, SALES)
    sim = FakeRun(core)
    sim.begin(T0)
    sim.run(10)  # Catchment offline the whole time: never flushed.

    # The run still completed and is durable in the ledger despite no Catchment contact.
    assert ledger.read_pond_end_f(con) == T0
    # Every ripple + the run completion are buffered, in order, awaiting delivery.
    assert [e.kind for e in core.events] == ["ripple", "ripple", "ripple", "run_completed"]

    # Reconnect: a flaky sink that fails once then accepts everything → buffer drains fully.
    delivered: list[dict] = []
    state = {"first": True}

    def sink(payload):
        if state["first"]:
            state["first"] = False
            return False  # one transient failure
        delivered.append(payload)
        return True

    core.flush(sink)  # first event fails, rest deliver
    core.flush(sink)  # retry delivers the straggler
    assert not core.events
    assert [d["kind"] for d in delivered] == ["ripple", "ripple", "ripple", "run_completed"]


@pytest.mark.timeout(5)
def test_duck_recovery_reruns_only_incomplete(tmp_path):
    # First Duck dies after daily+tiers complete but before join.
    con = ledger.connect(tmp_path / "pond.db")
    core = DuckCore("sales", con, SALES)
    sim = FakeRun(core)
    sim.begin(T0)
    sim.run(2.5)  # daily (2s) + tiers (1s) done; join (3s) still running
    assert core.state.states["daily"].end_f == T0
    assert core.state.states["join"].end_f != T0  # not yet complete

    # New Duck boots from the same ledger and is re-sent begin_run(T0) by the Catchment.
    con2 = ledger.connect(tmp_path / "pond.db")
    core2 = DuckCore("sales", con2, SALES)
    sim2 = FakeRun(core2)
    launched = core2.begin_run(T0, sim2.now)
    # Only join re-runs; daily/tiers are already complete for F.
    assert launched == ["join"]
    sim2.launch(launched)
    sim2.run(4)
    assert ledger.read_pond_end_f(con2) == T0
