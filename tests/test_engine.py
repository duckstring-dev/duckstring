"""Behavioural tests for the pure orchestration engine (``duckstring.engine``).

The engine has no run durations and no clock of its own — it is told when runs finish. So these
tests own a small :class:`Driver` that plays the future orchestrator: it holds sim-time ``now`` and
per-Ripple durations, launches whatever ``sentinel`` reports, and feeds completions back. Sim-time is
just the ``now`` argument, fully decoupled from wall-clock — the loop never sleeps. A 120 s sim is
~1200 pure calls and runs in milliseconds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from duckstring.engine import (
    EngineState,
    Pond,
    PondState,
    Ripple,
    RippleState,
    Trigger,
    Window,
    complete_ripple,
    next_wake,
    pond_set_has_pull,
    pond_source_f,
    pulse_pond,
    ripple_source_f,
    sentinel,
    stop_pond,
    tap_pond,
    tick,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
STEP = timedelta(milliseconds=100)


def secs(x: float) -> timedelta:
    return timedelta(seconds=x)


# ─── Topology construction ────────────────────────────────────────────────────


def build(ponds: list[Pond], ripples: list[Ripple], triggers: list[Trigger] | None = None) -> EngineState:
    return EngineState(
        ponds={p.id: p for p in ponds},
        pond_states={p.id: PondState() for p in ponds},
        ripples={r.id: r for r in ripples},
        ripple_states={r.id: RippleState() for r in ripples},
        triggers={t.pond_id: t for t in (triggers or [])},
    )


def chain_topology() -> tuple[EngineState, dict[str, timedelta]]:
    """p1 (inlet: r1, r2 -> r3) -> p2 (single ripple s1). All Ripples 1 s."""
    ponds = [Pond("p1", "p1"), Pond("p2", "p2", sources=["p1"])]
    ripples = [
        Ripple("r1", "p1", "r1"),
        Ripple("r2", "p1", "r2"),
        Ripple("r3", "p1", "r3", parents=["r1", "r2"]),
        Ripple("s1", "p2", "s1"),
    ]
    durations = {"r1": secs(1), "r2": secs(1), "r3": secs(1), "s1": secs(1)}
    return build(ponds, ripples), durations


def demo_topology(triggers: list[Trigger] | None = None) -> tuple[EngineState, dict[str, timedelta]]:
    """The reference demo: lead ~7 s, bottleneck (sales.join) = 3 s.

    transactions[1s] (inlet) ─┐
    products[2s]     (inlet) ─┴→ sales[daily 2s, tiers 1s (roots) → join 3s (leaf)] → reports[1s]
    """
    ponds = [
        Pond("transactions", "transactions"),
        Pond("products", "products"),
        Pond("sales", "sales", sources=["transactions", "products"]),
        Pond("reports", "reports", sources=["sales"]),
    ]
    ripples = [
        Ripple("tx", "transactions", "ingest"),
        Ripple("pr", "products", "ingest"),
        Ripple("daily", "sales", "daily"),
        Ripple("tiers", "sales", "tiers"),
        Ripple("join", "sales", "join", parents=["daily", "tiers"]),
        Ripple("monthly", "reports", "monthly"),
    ]
    durations = {
        "tx": secs(1),
        "pr": secs(2),
        "daily": secs(2),
        "tiers": secs(1),
        "join": secs(3),
        "monthly": secs(1),
    }
    return build(ponds, ripples, triggers), durations


def diamond_topology(triggers: list[Trigger] | None = None) -> tuple[EngineState, dict[str, timedelta]]:
    """Ponds S -> A, S -> B, and X consuming both A and B. One Ripple each, all 1 s."""
    ponds = [
        Pond("S", "S"),
        Pond("A", "A", sources=["S"]),
        Pond("B", "B", sources=["S"]),
        Pond("X", "X", sources=["A", "B"]),
    ]
    ripples = [Ripple(n.lower(), n, n) for n in ("S", "A", "B", "X")]
    durations = {"s": secs(1), "a": secs(1), "b": secs(1), "x": secs(1)}
    return build(ponds, ripples, triggers), durations


# ─── Driver (plays the orchestrator) ──────────────────────────────────────────


class Driver:
    def __init__(self, state: EngineState, durations: dict[str, timedelta], now: datetime = T0):
        self.state = state
        self.durations = durations
        self.now = now
        self.inflight: dict[str, datetime] = {}  # rid -> scheduled completion time

    def _react(self) -> None:
        self.state, started = sentinel(self.now, self.state)
        for rid in started:
            self.inflight[rid] = self.now + self.durations[rid]

    def tap(self, pid: str) -> None:
        self.state = tap_pond(self.state, pid, self.now)
        self._react()

    def pulse(self, pid: str) -> None:
        self.state = pulse_pond(self.state, pid, self.now)
        self._react()

    def stop(self, pid: str) -> None:
        self.state = stop_pond(self.state, pid, self.now)
        # A real stop also cancels any standing trigger on the path.
        self.state.triggers = {k: v for k, v in self.state.triggers.items() if k != pid}
        self._react()

    def step(self) -> None:
        due = [rid for rid, t in self.inflight.items() if t <= self.now]
        for rid in due:
            self.state = complete_ripple(self.state, rid, self.now)
            del self.inflight[rid]
        self.state = tick(self.now, self.state)
        self._react()
        self.now += STEP

    def run(self, seconds: float, stop_when=None) -> None:
        steps = int(seconds / 0.1)
        for _ in range(steps):
            self.step()
            if stop_when is not None and stop_when(self.state):
                return

    def run_until_completions(self, pid: str, n: int, max_seconds: float) -> None:
        self.run(max_seconds, stop_when=lambda s: s.pond_states[pid].runs_completed >= n)


def gaps_seconds(times: list[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]


# ─── Unit tests (pure helpers) ────────────────────────────────────────────────


@pytest.mark.timeout(1)
def test_pond_source_f_live_inlet():
    s, _ = chain_topology()
    f, d = pond_source_f(s, "p1", T0)
    assert f == T0 and d == timedelta(0)


@pytest.mark.timeout(1)
def test_pond_source_f_windowed():
    pond = Pond("w", "w", windows=[Window("* * * * *", secs(20))])
    s = build([pond], [Ripple("wr", "w", "wr")])
    # Inside the window [00:00:00, 00:00:20): F = window end, D = duration.
    f, d = pond_source_f(s, "w", T0 + secs(5))
    assert f == T0 + secs(20) and d == secs(20)
    # In the gap [00:00:20, 00:01:00): cannot run.
    f, d = pond_source_f(s, "w", T0 + secs(30))
    assert f is None


@pytest.mark.timeout(1)
def test_pond_source_f_required_vs_optional():
    a, b = PondState(end_f=T0 + secs(5)), PondState(end_f=T0 + secs(2))
    base = Pond("c", "c", sources=["a", "b"])
    s = build([Pond("a", "a"), Pond("b", "b"), base], [])
    s.pond_states["a"], s.pond_states["b"] = a, b
    # Both required: stalest (min) wins.
    f, _ = pond_source_f(s, "c", T0)
    assert f == T0 + secs(2)
    # b optional: freshest required (a) wins... with only-optional it would be max.
    s.ponds["c"].optional_sources = {"b"}
    f, _ = pond_source_f(s, "c", T0)
    assert f == T0 + secs(5)


@pytest.mark.timeout(1)
def test_ripple_source_f_root_and_parents():
    s, _ = chain_topology()
    s.pond_states["p1"].start_f = T0 + secs(3)
    assert ripple_source_f(s, "r1") == T0 + secs(3)  # root → pond.start_f
    s.ripple_states["r1"].end_f = T0 + secs(4)
    s.ripple_states["r2"].end_f = T0 + secs(1)
    assert ripple_source_f(s, "r3") == T0 + secs(1)  # required min
    s.ripples["r3"].optional_parents = {"r2"}
    assert ripple_source_f(s, "r3") == T0 + secs(4)  # only r1 required


@pytest.mark.timeout(1)
def test_startf_propagation_guard():
    # A source already running ahead (start_f > child.start_f) is NOT re-armed by a pull cascade.
    s, _ = chain_topology()
    s.pond_states["p1"].start_f = T0 + secs(5)  # p1 running ahead
    s.pond_states["p1"].end_f = T0 + secs(4)
    s.pond_states["p2"].start_f = T0 + secs(1)  # p2 behind
    pond_set_has_pull(s, "p2", T0 + secs(10))
    assert not s.pond_states["p1"].has_pull  # skipped — its in-flight run will satisfy the demand


@pytest.mark.timeout(1)
def test_next_wake_window_and_tide():
    pond = Pond("w", "w", windows=[Window("* * * * *", secs(20))])
    s = build([pond], [Ripple("wr", "w", "wr")])
    # Inside the window → next wake is its close at 00:00:20.
    assert next_wake(T0 + secs(5), s) == T0 + secs(20)
    # In the gap → next wake is the next open at 00:01:00.
    assert next_wake(T0 + secs(30), s) == T0 + secs(60)
    # Tide: deadline = reference (start_f) + bound.
    s2 = build([Pond("p", "p")], [Ripple("pr", "p", "pr")], [Trigger("p", "tide", secs(10))])
    s2.pond_states["p"].start_f = T0
    assert next_wake(T0 + secs(2), s2) == T0 + secs(10)


# ─── Simulation tests ─────────────────────────────────────────────────────────


@pytest.mark.timeout(5)
def test_tap_two_pond_chain():
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.tap("p2")
    d.run(20)
    assert d.state.pond_states["p1"].runs_completed == 3
    assert d.state.pond_states["p2"].runs_completed == 1


@pytest.mark.timeout(5)
def test_pulse_two_pond_chain_coherent():
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.pulse("p2")
    d.run(20)
    p1, p2 = d.state.pond_states["p1"], d.state.pond_states["p2"]
    assert p1.runs_completed == 1
    assert p2.runs_completed == 1
    assert p1.end_f == p2.end_f  # whole chain at one coherent freshness


@pytest.mark.timeout(5)
def test_wave_settles_to_bottleneck():
    s, dur = demo_topology([Trigger("reports", "wave")])
    d = Driver(s, dur)
    d.run_until_completions("reports", 6, max_seconds=60)
    # Steady-state completion cadence == the 3 s bottleneck (join), inlets included (no over-pull).
    for pid in ("reports", "sales", "transactions", "products"):
        g = gaps_seconds(d.state.pond_states[pid].completion_times)[-3:]
        assert g, f"{pid} did not complete enough times"
        for gap in g:
            assert abs(gap - 3.0) < 0.2, f"{pid} cadence {g} != 3.0s"


@pytest.mark.timeout(5)
@pytest.mark.parametrize("bound,expected", [(3, 3.0), (4, 4.0), (7, 7.0), (10, 10.0), (20, 20.0)])
def test_tide_cadence_matches_bound(bound, expected):
    s, dur = demo_topology([Trigger("reports", "tide", secs(bound))])
    d = Driver(s, dur)
    d.run_until_completions("reports", 4, max_seconds=10 + 3.5 * bound)
    g = gaps_seconds(d.state.pond_states["reports"].completion_times)[-2:]
    assert g, "reports did not complete enough times"
    for gap in g:
        assert abs(gap - expected) < 0.25, f"bound {bound}: cadence {g} != {expected}"


@pytest.mark.timeout(5)
def test_tide_below_bottleneck_throttles():
    # A 2 s bound is below the 3 s bottleneck → completions throttle to 3 s, pulses pipeline.
    s, dur = demo_topology([Trigger("reports", "tide", secs(2))])
    d = Driver(s, dur)
    max_targets = 0

    def watch(state):
        nonlocal max_targets
        max_targets = max(max_targets, *(len(ps.targets) for ps in state.pond_states.values()))
        return state.pond_states["reports"].runs_completed >= 5

    d.run(60, stop_when=watch)
    g = gaps_seconds(d.state.pond_states["reports"].completion_times)[-2:]
    for gap in g:
        assert abs(gap - 3.0) < 0.3, f"throttled cadence {g} != 3.0s"
    assert max_targets > 1  # several Pulses in flight at once (pipelining)


@pytest.mark.timeout(5)
def test_push_precision_diamond():
    # Standing Wave on B churns it; one Pulse on X must run X exactly once despite the forked path.
    s, dur = diamond_topology([Trigger("B", "wave")])
    d = Driver(s, dur)
    d.run(2)  # let the Wave on B get going
    d.pulse("X")
    d.run(20)
    assert d.state.pond_states["X"].runs_completed == 1
    assert d.state.pond_states["B"].runs_completed > 3  # B churned many times


@pytest.mark.timeout(5)
def test_windowed_inlet_throttles_chain():
    inlet = Pond("w", "w", windows=[Window("* * * * *", secs(20))])
    child = Pond("c", "c", sources=["w"])
    state = build([inlet, child], [Ripple("wr", "w", "wr"), Ripple("cr", "c", "cr")], [Trigger("c", "wave")])
    d = Driver(state, {"wr": secs(1), "cr": secs(1)})
    d.run(150)
    # One run per minute window → child completions ~60 s apart.
    g = gaps_seconds(d.state.pond_states["c"].completion_times)
    assert g, "child never completed"
    assert all(abs(gap - 60.0) < 0.5 for gap in g), f"chain not throttled to window period: {g}"
    # The windowed inlet runs at most once per window.
    assert d.state.pond_states["w"].runs_completed <= 3


@pytest.mark.timeout(5)
def test_stop_clears_demand_and_drains():
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.state.triggers = {"p2": Trigger("p2", "wave")}
    d.run(10)  # run a few cycles
    assert d.state.pond_states["p2"].runs_completed > 1
    d.stop("p2")
    # Demand cleared everywhere up the ancestry.
    for pid in ("p1", "p2"):
        ps = d.state.pond_states[pid]
        assert not ps.has_pull and not ps.has_received_pull and not ps.targets
    for rid in ("r1", "r2", "r3", "s1"):
        rs = d.state.ripple_states[rid]
        assert not rs.has_pull and not rs.targets
    # In-flight runs drain, then nothing new starts.
    d.run(5)
    settled = d.state.pond_states["p2"].runs_completed
    d.run(10)
    assert d.state.pond_states["p2"].runs_completed == settled
