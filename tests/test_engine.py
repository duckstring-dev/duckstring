"""Behavioural tests for the pure orchestration engine (``duckstring.engine``).

The engine has no run durations and no clock of its own — it is told when runs finish. So these
tests own a small :class:`Driver` that plays the future orchestrator: it holds sim-time ``now`` and
per-Ripple durations, launches whatever ``sentinel`` reports, and feeds completions back. Sim-time is
just the ``now`` argument, fully decoupled from wall-clock — the loop never sleeps. A 120 s sim is
~1200 pure calls and runs in milliseconds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from duckstring.engine import (
    NEVER,
    EngineState,
    Pond,
    PondState,
    Refresh,
    Ripple,
    RippleState,
    ThrottleState,
    Window,
    add_demand,
    block_on_missing_asset,
    clear_missing_asset,
    clear_pond,
    complete_ripple,
    demand_sources,
    derive_blocked,
    fail_pond,
    fail_ripple,
    force_pond,
    is_throttled,
    kill_pond,
    next_wake,
    pond_source_f,
    refresh_next_due,
    refresh_now,
    ripple_source_f,
    sentinel,
    sleep_pond,
    tick,
    wake_pond,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
STEP = timedelta(milliseconds=100)


def secs(x: float) -> timedelta:
    return timedelta(seconds=x)


# ─── Topology construction ────────────────────────────────────────────────────


def build(ponds: list[Pond], ripples: list[Ripple], refreshes: list[Refresh] | None = None) -> EngineState:
    by_pond: dict[str, list[Refresh]] = {}
    for r in refreshes or []:
        by_pond.setdefault(r.pond_id, []).append(r)
    return EngineState(
        ponds={p.id: p for p in ponds},
        pond_states={p.id: PondState() for p in ponds},
        ripples={r.id: r for r in ripples},
        ripple_states={r.id: RippleState() for r in ripples},
        refreshes=by_pond,
    )


def floating(pid: str, seconds: float) -> Refresh:
    """A floating Refresh every ``seconds`` (0 = continuous)."""
    return Refresh(pid, "continuous" if seconds == 0 else f"every {seconds}", every=secs(seconds))


def continuous(pid: str) -> Refresh:
    return floating(pid, 0)


def test_standing_wake_delivers_on_source_advance_without_soliciting():
    """A Spout node (standing Wake): runs whenever its Source is fresher, never solicits the Source
    (its demand is local), and does not re-run until the Source advances again."""
    src = Pond("src", "src")
    spt = Pond("spt", "spt", sources=["src"], is_spout=True)  # terminal, no Ripples — run by the worker
    s = build([src, spt], [Ripple("r", "src", "r")])
    s.pond_states["spt"].standing_wake = True

    s.pond_states["src"].start_f = s.pond_states["src"].end_f = T0  # the Source publishes a freshness
    s = tick(T0, s)
    s, _ = sentinel(T0, s)
    assert s.pond_states["spt"].start_f == T0                       # delivered at the Source's freshness
    assert s.pond_states["src"].demand == []                        # a Wake never solicits the Source
    assert "spt" in [b.pond_id for b in s.pending_begin_runs]

    # Complete the delivery; the Source isn't fresher → it does not re-run.
    s.pond_states["spt"].end_f = s.pond_states["spt"].start_f
    s.pending_begin_runs.clear()
    s = tick(T0 + STEP, s)
    s, _ = sentinel(T0 + STEP, s)
    assert s.pending_begin_runs == []

    # The Source advances → it delivers again.
    s.pond_states["src"].start_f = s.pond_states["src"].end_f = T0 + secs(10)
    s = tick(T0 + secs(10), s)
    s, _ = sentinel(T0 + secs(10), s)
    assert s.pond_states["spt"].start_f == T0 + secs(10)
    assert "spt" in [b.pond_id for b in s.pending_begin_runs]


def test_windowed_spout_fires_once_per_window():
    """A windowed Spout uses the same window mechanics as an Inlet: it fires once per window, stamped
    with the window end (the throttle clock), not the source freshness."""
    win = Window(start_anchor=T0, duration=timedelta(days=1), freq_unit="DAY", freq_interval=1)  # back-to-back daily
    spt = Pond("spt", "spt", sources=["src"], is_spout=True, windows=[win])
    s = build([Pond("src", "src"), spt], [])
    s.pond_states["spt"].standing_wake = True
    s.pond_states["src"].start_f = s.pond_states["src"].end_f = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

    at = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    s = tick(at, s)
    s, _ = sentinel(at, s)
    assert s.pond_states["spt"].start_f == datetime(2026, 6, 11, 0, 0, tzinfo=UTC)  # the window end, not 12:00
    assert "spt" in [b.pond_id for b in s.pending_begin_runs]

    # Later in the same window → no re-fire (window end unchanged).
    s.pond_states["spt"].end_f = s.pond_states["spt"].start_f
    s.pending_begin_runs.clear()
    later = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    s = tick(later, s)
    s, _ = sentinel(later, s)
    assert s.pending_begin_runs == []

    # Next window → fires again, stamped with the next window's end.
    nxt = datetime(2026, 6, 11, 1, 0, tzinfo=UTC)
    s.pond_states["src"].end_f = nxt
    s = tick(nxt, s)
    s, _ = sentinel(nxt, s)
    assert s.pond_states["spt"].start_f == datetime(2026, 6, 12, 0, 0, tzinfo=UTC)


def test_windowed_spout_holds_in_gap_and_before_source():
    future = Window(start_anchor=datetime(2030, 1, 1, tzinfo=UTC), duration=timedelta(hours=1),
                    freq_unit="DAY", freq_interval=1)  # opens only in 2030 → a gap now
    spt = Pond("spt", "spt", sources=["src"], is_spout=True, windows=[future])
    s = build([Pond("src", "src"), spt], [])
    s.pond_states["spt"].standing_wake = True
    s.pond_states["src"].end_f = datetime(2026, 6, 10, tzinfo=UTC)
    at = datetime(2026, 6, 10, 12, tzinfo=UTC)
    s = tick(at, s)
    s, _ = sentinel(at, s)
    assert s.pending_begin_runs == []  # in a window gap → delivery holds

    # An active window but the source has never published → still nothing to deliver.
    live = Window(start_anchor=T0, duration=timedelta(days=1), freq_unit="DAY", freq_interval=1)
    spt2 = Pond("spt2", "spt2", sources=["src2"], is_spout=True, windows=[live])
    s2 = build([Pond("src2", "src2"), spt2], [])
    s2.pond_states["spt2"].standing_wake = True  # src2.end_f stays NEVER
    s2 = tick(at, s2)
    s2, _ = sentinel(at, s2)
    assert s2.pending_begin_runs == []


def test_standing_wake_parked_when_failed_or_killed():
    spt = Pond("spt", "spt", sources=["src"], is_spout=True)
    s = build([Pond("src", "src"), spt], [])
    s.pond_states["spt"].standing_wake = True
    s.pond_states["src"].start_f = s.pond_states["src"].end_f = T0
    for flag in ("is_failed", "is_killed"):
        st = s.clone()
        setattr(st.pond_states["spt"], flag, True)
        st = tick(T0, st)
        st, _ = sentinel(T0, st)
        assert st.pending_begin_runs == []  # parked → no delivery


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


def demo_topology(refreshes: list[Refresh] | None = None) -> tuple[EngineState, dict[str, timedelta]]:
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
    return build(ponds, ripples, refreshes), durations


def diamond_topology(refreshes: list[Refresh] | None = None) -> tuple[EngineState, dict[str, timedelta]]:
    """Ponds S -> A, S -> B, and X consuming both A and B. One Ripple each, all 1 s."""
    ponds = [
        Pond("S", "S"),
        Pond("A", "A", sources=["S"]),
        Pond("B", "B", sources=["S"]),
        Pond("X", "X", sources=["A", "B"]),
    ]
    ripples = [Ripple(n.lower(), n, n) for n in ("S", "A", "B", "X")]
    durations = {"s": secs(1), "a": secs(1), "b": secs(1), "x": secs(1)}
    return build(ponds, ripples, refreshes), durations


def graph(spec: list[tuple[str, float | list[float], list[str]]]) -> tuple[EngineState, dict[str, timedelta]]:
    """Ponds as ``(name, seconds or a sequence of Ripples' seconds, sources)``; a Pond's Ripples run in
    sequence as ``{name}.r{i}``. The shape of the playground's throttle tests."""
    ponds, ripples, durations = [], [], {}
    for name, sec, sources in spec:
        ponds.append(Pond(name, name, sources=list(sources)))
        for i, d in enumerate(sec if isinstance(sec, list) else [sec]):
            rid = f"{name}.r{i}"
            ripples.append(Ripple(rid, name, f"r{i}", parents=[f"{name}.r{i - 1}"] if i else []))
            durations[rid] = secs(d)
    return build(ponds, ripples), durations


def chain(*spec: tuple[str, float | list[float]]) -> tuple[EngineState, dict[str, timedelta]]:
    """A linear :func:`graph`, each Pond reading the one before it."""
    return graph([(name, sec, [spec[i - 1][0]] if i else []) for i, (name, sec) in enumerate(spec)])


# ─── Driver (plays the orchestrator) ──────────────────────────────────────────


class Driver:
    def __init__(self, state: EngineState, durations: dict[str, timedelta], now: datetime = T0,
                 drift: bool = False):
        self.state = state
        self.durations = durations
        self.now = now
        self.inflight: dict[str, datetime] = {}  # rid -> scheduled completion time
        self.fail_counts: dict[str, int] = {}  # rid -> remaining runs to fail instead of complete
        self.unchanged: set[str] = set()  # rids whose completions report no output change (changed=False)
        # Drift: each step advances 90 to 129 ms instead of 100, from the playground's seeded generator, as
        # its real-time ticks drift. Events that coincide on an exact grid then land in different steps.
        self.drift = drift
        self._seed = 7

    def advance(self) -> None:
        """Move sim time on by one step (with drift, if enabled)."""
        jitter = 0
        if self.drift:
            self._seed = (self._seed * 1664525 + 1013904223) & 0xFFFFFFFF
            jitter = self._seed % 40 - 10
        self.now += STEP + timedelta(milliseconds=jitter)

    def fail_next(self, rid: str, n: int = 1) -> None:
        """Make the next ``n`` runs of ``rid`` error (the Duck gave up) rather than complete."""
        self.fail_counts[rid] = self.fail_counts.get(rid, 0) + n

    def mark_unchanged(self, rid: str) -> None:
        """Make ``rid``'s completions report no output change (a Trickle empty delta / pond.skip), so
        the Pond holds its ``changed_f`` — simulating a Duck reporting ``changed=False``."""
        self.unchanged.add(rid)

    def _react(self) -> None:
        self.state, started = sentinel(self.now, self.state)
        for rid in started:
            self.inflight[rid] = self.now + self.durations[rid]

    def refresh(self, pid: str) -> None:
        self.state = refresh_now(self.state, pid, self.now)
        self._react()

    def stop(self, pid: str, upstream: bool = False) -> None:
        self.state = sleep_pond(self.state, pid, self.now, upstream=upstream)
        # Halt for the test: also drop any standing Refresh so it can't ask again (the Driver does this).
        self.state.refreshes = {k: v for k, v in self.state.refreshes.items() if k != pid}
        self._react()

    def step(self) -> None:
        self.process()
        self.advance()

    def process(self) -> None:
        """At ``now``: feed back every due completion (in Ripple order), tick, and react."""
        due = [rid for rid in self.state.ripple_states if rid in self.inflight and self.inflight[rid] <= self.now]
        for rid in due:
            del self.inflight[rid]
            if self.fail_counts.get(rid, 0) > 0:
                self.fail_counts[rid] -= 1
                self.state = fail_ripple(self.state, rid, self.now)
            else:
                self.state = complete_ripple(self.state, rid, self.now, changed=rid not in self.unchanged)
        self.state = tick(self.now, self.state)
        self._react()

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
    pond = Pond("w", "w", windows=[Window(start_anchor=T0, duration=secs(20), freq_unit="MINUTE", freq_interval=1)])
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
def test_inlet_stamps_demand_epoch_not_run_now():
    # Minted freshness: an Inlet that runs LATER than the request that demanded it stamps the demand
    # epoch, not its run-now. (Same-tick runs are unchanged because now == the epoch.)
    s = build([Pond("i", "i")], [Ripple("ir", "i", "ir")])
    epoch = T0 + secs(5)
    s.pond_states["i"].demand = [epoch]
    s2, started = sentinel(T0 + secs(10), s)  # runs at T0+10, but the demand epoch is T0+5
    assert "ir" in started
    assert s2.pond_states["i"].start_f == epoch


@pytest.mark.timeout(1)
def test_force_inlet_ignores_never_sentinel_and_stamps_now():
    # Force adds a NEVER demand; it must be filtered out so the Inlet stamps `now`, not datetime.min.
    s = build([Pond("i", "i")], [Ripple("ir", "i", "ir")])
    s = force_pond(s, "i", T0)
    s2, _ = sentinel(T0 + secs(3), s)
    assert s2.pond_states["i"].start_f == T0 + secs(3)


@pytest.mark.timeout(1)
def test_completion_prunes_demand_added_mid_run():
    # Demand added during a Run (valid then: t > end_f) must not linger past completion to fire a
    # second Run at the same freshness.
    pond = Pond("p", "p")
    s = build([pond], [Ripple("r", "p", "r")])
    T = T0 + secs(5)
    s.pond_states["p"].start_f = T  # mid-run at freshness T
    s.pond_states["p"].end_f = T0
    s.ripple_states["r"].start_f = T
    s.ripple_states["r"].is_running = True
    s.pond_states["p"].demand = [T]  # a duplicate request arrived during the Run

    s = complete_ripple(s, "r", T0 + secs(6))
    assert s.pond_states["p"].end_f == T
    assert s.pond_states["p"].demand == []  # satisfied demand pruned
    _, started = sentinel(T0 + secs(7), s)
    assert started == []  # no spurious re-run


@pytest.mark.timeout(2)
def test_refresh_freshness_uniform_across_staggered_diamond():
    # S -> A, S -> B, X <- A,B with B slower: a Refresh reaches all; every node ends at its epoch.
    s, durations = diamond_topology()
    durations["b"] = secs(3)
    d = Driver(s, durations)
    d.refresh("X")
    d.run(10)
    for pid in ("S", "A", "B", "X"):
        assert d.state.pond_states[pid].end_f == T0


@pytest.mark.timeout(1)
def test_next_wake_window_and_floating_refresh():
    pond = Pond("w", "w", windows=[Window(start_anchor=T0, duration=secs(20), freq_unit="MINUTE", freq_interval=1)])
    s = build([pond], [Ripple("wr", "w", "wr")])
    # Inside the window → next wake is its close at 00:00:20.
    assert next_wake(T0 + secs(5), s) == T0 + secs(20)
    # In the gap → next wake is the next open at 00:01:00.
    assert next_wake(T0 + secs(30), s) == T0 + secs(60)
    # A floating Refresh: deadline = reference (start_f) + every.
    s2 = build([Pond("p", "p")], [Ripple("pr", "p", "pr")], [floating("p", 10)])
    s2.pond_states["p"].start_f = T0
    assert next_wake(T0 + secs(2), s2) == T0 + secs(10)


# ─── Simulation tests ─────────────────────────────────────────────────────────


@pytest.mark.timeout(5)
def test_refresh_two_pond_chain_coherent():
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.refresh("p2")
    d.run(20)
    p1, p2 = d.state.pond_states["p1"], d.state.pond_states["p2"]
    assert p1.runs_completed == 1
    assert p2.runs_completed == 1
    assert p1.end_f == p2.end_f  # whole chain at one coherent freshness


@pytest.mark.timeout(5)
def test_continuous_refresh_settles_to_bottleneck():
    s, dur = demo_topology([continuous("reports")])
    d = Driver(s, dur)
    d.run_until_completions("reports", 6, max_seconds=60)
    # Steady-state completion cadence == the 3 s bottleneck (join), Inlets included (no over-asking).
    for pid in ("reports", "sales", "transactions", "products"):
        g = gaps_seconds(d.state.pond_states[pid].completion_times)[-3:]
        assert g, f"{pid} did not complete enough times"
        for gap in g:
            assert abs(gap - 3.0) < 0.2, f"{pid} cadence {g} != 3.0s"


@pytest.mark.timeout(5)
@pytest.mark.parametrize("bound,expected", [(3, 3.0), (4, 4.0), (7, 7.0), (10, 10.0), (20, 20.0)])
def test_floating_refresh_cadence_matches_every(bound, expected):
    s, dur = demo_topology([floating("reports", bound)])
    d = Driver(s, dur)
    d.run_until_completions("reports", 4, max_seconds=10 + 3.5 * bound)
    g = gaps_seconds(d.state.pond_states["reports"].completion_times)[-2:]
    assert g, "reports did not complete enough times"
    for gap in g:
        assert abs(gap - expected) < 0.25, f"bound {bound}: cadence {g} != {expected}"


@pytest.mark.timeout(5)
def test_floating_refresh_below_bottleneck_throttles():
    # Every 2 s is below the 3 s bottleneck → completions throttle to 3 s, requests pipeline.
    s, dur = demo_topology([floating("reports", 2)])
    d = Driver(s, dur)
    max_demand = 0

    def watch(state):
        nonlocal max_demand
        max_demand = max(max_demand, *(len(ps.demand) for ps in state.pond_states.values()))
        return state.pond_states["reports"].runs_completed >= 5

    d.run(60, stop_when=watch)
    g = gaps_seconds(d.state.pond_states["reports"].completion_times)[-2:]
    for gap in g:
        assert abs(gap - 3.0) < 0.3, f"throttled cadence {g} != 3.0s"
    assert max_demand > 1  # several requests in flight at once (pipelining)


# ─── Scheduled Refreshes ──────────────────────────────────────────────────────


def scheduled(pid: str, start: datetime, period: float, **kw) -> Refresh:
    return Refresh(pid, f"at {start:%H:%M:%S}", every=secs(period), start=start, **kw)


@pytest.mark.timeout(5)
def test_scheduled_refresh_requests_each_occurrence_at_its_epoch():
    """A scheduled Refresh asks for each grid occurrence; the Inlet stamps the occurrence, so the data is
    labelled with the schedule's time."""
    s, dur = chain(("I", 1), ("O", 1))
    s.refreshes = {"O": [scheduled("O", T0 + secs(10), 60)]}
    d = Driver(s, dur)
    d.run(200)
    o = d.state.pond_states["O"]
    assert o.runs_completed == 4  # T0+10, +70, +130, +190
    assert o.end_f == T0 + secs(190)
    assert [round((t - T0).total_seconds()) for t in o.completion_times] == [12, 72, 132, 192]


@pytest.mark.timeout(5)
def test_scheduled_refresh_catches_up_after_downtime_with_one_request():
    """A Catchment that was down through several occurrences asks once, for the latest, when it returns."""
    s, dur = chain(("I", 1), ("O", 1))
    s.refreshes = {"O": [scheduled("O", T0, 60)]}
    d = Driver(s, dur, now=T0 + secs(250))  # first tick after four missed occurrences
    d.run(5)
    o = d.state.pond_states["O"]
    assert o.runs_completed == 1 and o.end_f == T0 + secs(240)


@pytest.mark.timeout(1)
def test_scheduled_refresh_days_and_until():
    monday = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)  # a Monday, 06:00
    r = scheduled("O", monday, 86400, valid_days=frozenset({"MON", "TUE", "WED", "THU", "FRI"}))
    saturday = monday + timedelta(days=5, hours=1)
    assert r.latest(saturday) is None  # Saturday's own occurrence is filtered out
    assert r.latest(monday + timedelta(days=4, hours=2)) == monday + timedelta(days=4)  # Friday 06:00
    assert r.next_occurrence(saturday) == monday + timedelta(days=7)  # next Monday
    assert r.latest(monday - secs(1)) is None  # before its start

    ending = scheduled("O", monday, 86400, until=monday + timedelta(days=1, hours=12))
    assert ending.latest(monday + timedelta(days=1, hours=1)) == monday + timedelta(days=1)
    assert ending.next_occurrence(monday + timedelta(days=1, hours=1)) is None  # ended


@pytest.mark.timeout(5)
def test_a_manual_refresh_resets_a_floating_refresh_but_not_a_scheduled_one():
    s, dur = chain(("I", 1), ("O", 1))
    every60, at30 = floating("O", 60), scheduled("O", T0 + secs(30), 60)
    s.refreshes = {"O": [every60, at30]}  # several standing Refreshes on one Pond
    d = Driver(s, dur)
    d.run(10)  # the floating one asks at T0 (the Pond is unbuilt)
    d.refresh("O")  # a manual refresh at T0+10
    d.run(5)
    assert refresh_next_due(d.state, every60, d.now) == T0 + secs(70)  # 60 s after the manual refresh
    assert refresh_next_due(d.state, at30, d.now) == T0 + secs(30)  # the schedule is unmoved
    assert next_wake(d.now, d.state) == T0 + secs(30)
    d.run(85)  # to T0+100
    # The scheduled run at T0+30 is a refresh too, so it moves the floating one to T0+90, where the two
    # coincide in one request.
    o = d.state.pond_states["O"]
    assert [round((t - T0).total_seconds()) for t in o.completion_times] == [2, 12, 32, 92]


@pytest.mark.timeout(1)
def test_a_continuous_refresh_is_due_on_every_tick():
    s, _ = chain(("I", 1), ("O", 1))
    c = continuous("O")
    s.refreshes = {"O": [c]}
    assert refresh_next_due(s, c, T0) is None
    assert next_wake(T0, s) == T0


@pytest.mark.timeout(5)
def test_missing_source_asset_blocks_with_reason_not_failed():
    """A read of an unpublished Source asset (plans/reset.md Mechanism 2) parks the Pond blocked-with-a-
    reason — not failed: no budget burn, no ``failed_f`` — abandons the incomplete Run, and propagates
    ``blocked`` downstream. ``clear_missing_asset`` (a later clean read) releases it and the downstream."""
    src = Pond("src", "src")
    mid = Pond("mid", "mid", sources=["src"])
    dwn = Pond("dwn", "dwn", sources=["mid"])
    s = build([src, mid, dwn], [Ripple("r", "mid", "r"), Ripple("r2", "dwn", "r2")])
    # `mid` started a Run (reached start_f=T0) then read src.x and missed.
    s.pond_states["mid"].start_f = T0
    s.pond_states["mid"].end_f = NEVER
    s.ripple_states["r"].start_f = T0
    s.ripple_states["r"].is_running = True

    s = block_on_missing_asset(s, "mid", "src.x", T0 + STEP)
    m = s.pond_states["mid"]
    assert m.missing_asset == "src.x"
    assert m.is_blocked is True and m.is_failed is False   # blocked, not failed
    assert m.failures == 0 and m.failed_f == NEVER          # no budget burn
    assert m.start_f == m.end_f                             # incomplete Run abandoned (liveness won't fail it)
    assert not s.ripple_states["r"].is_running
    assert s.pond_states["dwn"].is_blocked is True          # propagated downstream

    s = clear_missing_asset(s, "mid")                       # a later clean read
    assert s.pond_states["mid"].missing_asset is None
    assert s.pond_states["mid"].is_blocked is False
    assert s.pond_states["dwn"].is_blocked is False         # downstream released too


@pytest.mark.timeout(5)
def test_missing_source_asset_does_not_busy_loop_at_same_freshness():
    """A Pond parked on a missing Source asset must NOT re-dispatch a Run every tick while the Source
    freshness is unchanged — the rewound run would otherwise re-fire at the same freshness forever (a
    Duck-round-trip busy-loop that read-misses each time, floods run history, and starves the Catchment).
    It re-attempts only once the Source publishes something genuinely fresher. See plans/reset.md."""
    src = Pond("src", "src")
    mid = Pond("mid", "mid", sources=["src"])
    s = build([src, mid], [Ripple("r", "mid", "r")])
    s.pond_states["src"].start_f = s.pond_states["src"].end_f = T0  # the Source has a freshness on disk
    # `mid` ran at the Source's freshness (start_f=T0) then read src.x and missed.
    s.pond_states["mid"].start_f = T0
    s.ripple_states["r"].start_f = T0
    s.ripple_states["r"].is_running = True
    s = block_on_missing_asset(s, "mid", "src.x", T0 + STEP)
    assert s.pond_states["mid"].missing_asset_f == T0  # remembered the missed freshness

    # Drive many ticks over an UNCHANGED Source: `mid` must not re-run even once.
    for i in range(20):
        s = tick(T0 + (i + 2) * STEP, s)
        s, _ = sentinel(T0 + (i + 2) * STEP, s)
    assert [b.pond_id for b in s.pending_begin_runs] == [], "parked Pond busy-looped on the same freshness"
    assert s.pond_states["mid"].runs_started == 0

    # The Source advances → `mid` re-attempts exactly once.
    later = T0 + secs(10)
    s.pond_states["src"].start_f = s.pond_states["src"].end_f = later
    s = tick(later, s)
    s, _ = sentinel(later, s)
    assert [b.pond_id for b in s.pending_begin_runs] == ["mid"], "did not re-attempt on a genuine Source advance"


@pytest.mark.timeout(5)
def test_demand_precision_diamond():
    # A continuous Refresh on B churns it; one Refresh on X must run X exactly once despite the forked path.
    s, dur = diamond_topology([continuous("B")])
    d = Driver(s, dur)
    d.run(2)  # let the Refresh on B get going
    d.refresh("X")
    d.run(20)
    assert d.state.pond_states["X"].runs_completed == 1
    assert d.state.pond_states["B"].runs_completed > 3  # B churned many times


@pytest.mark.timeout(5)
def test_windowed_inlet_throttles_chain():
    inlet = Pond("w", "w", windows=[Window(start_anchor=T0, duration=secs(20), freq_unit="MINUTE", freq_interval=1)])
    child = Pond("c", "c", sources=["w"])
    state = build([inlet, child], [Ripple("wr", "w", "wr"), Ripple("cr", "c", "cr")], [continuous("c")])
    d = Driver(state, {"wr": secs(1), "cr": secs(1)})
    d.run(150)
    # One run per minute window → child completions ~60 s apart.
    g = gaps_seconds(d.state.pond_states["c"].completion_times)
    assert g, "child never completed"
    assert all(abs(gap - 60.0) < 0.5 for gap in g), f"chain not throttled to window period: {g}"
    # The windowed inlet runs at most once per window.
    assert d.state.pond_states["w"].runs_completed <= 3


@pytest.mark.timeout(1)
def test_stop_local_clears_demand_keeps_ripple_demand():
    # sleep_pond on the target Pond only: clears its demand, KEEPS its Ripples' demand (a started run
    # completes), and does not touch upstream.
    s, _ = chain_topology()
    far = T0 + secs(99)
    s.pond_states["p2"].demand = [far]
    s.ripple_states["s1"].demand = [far]
    s.pond_states["p1"].demand = [far]  # upstream demand
    out = sleep_pond(s, "p2", T0)
    assert not out.pond_states["p2"].demand
    assert out.ripple_states["s1"].demand == [far]       # Ripple demand kept (started run completes)
    assert out.pond_states["p1"].demand == [far]         # upstream untouched (no --upstream)


@pytest.mark.timeout(1)
def test_stop_upstream_propagates():
    s, _ = chain_topology()
    s.pond_states["p1"].demand = [T0 + secs(99)]
    s.pond_states["p2"].demand = [T0 + secs(99)]
    out = sleep_pond(s, "p2", T0, upstream=True)
    assert not out.pond_states["p2"].demand
    assert not out.pond_states["p1"].demand              # propagated to the source


@pytest.mark.timeout(5)
def test_stop_drains_then_halts():
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.state.refreshes = {"p2": [continuous("p2")]}
    d.run(10)  # run a few cycles
    assert d.state.pond_states["p2"].runs_completed > 1
    d.stop("p2", upstream=True)
    # In-flight runs drain (ripple push kept), then nothing new starts.
    d.run(5)
    settled = d.state.pond_states["p2"].runs_completed
    d.run(10)
    assert d.state.pond_states["p2"].runs_completed == settled


# ─── Fault tolerance ──────────────────────────────────────────────────────────


@pytest.mark.timeout(5)
def test_failure_without_retry_fails_and_blocks():
    # No retry budget: a Ripple giving up fails its Pond, which won't run again on its own.
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.fail_next("s1", 1)
    d.refresh("p2")
    d.run(20)
    p2 = d.state.pond_states["p2"]
    assert p2.is_failed and p2.is_blocked
    assert p2.failures == 1 and p2.runs_completed == 0
    assert not d.state.pond_states["p1"].is_failed  # no stop signal travels upstream


@pytest.mark.timeout(5)
def test_retry_on_change_recovers():
    # With budget 1 and a Source that keeps moving, a single failure is retried and recovers.
    s, dur = chain_topology()
    s.ponds["p2"].retry_on_change = 1
    s.refreshes["p1"] = [continuous("p1")]  # p1 advances on its own, independent of the failure
    d = Driver(s, dur)
    d.fail_next("s1", 1)  # only the first p2 run fails
    d.refresh("p2")
    d.run(30, stop_when=lambda st: st.pond_states["p2"].runs_completed >= 1)
    p2 = d.state.pond_states["p2"]
    assert p2.runs_completed >= 1
    assert not p2.is_failed and not p2.is_blocked and p2.failed_f == NEVER


@pytest.mark.timeout(5)
def test_retry_on_change_exhausts_to_terminal():
    # Budget 1, but every attempt fails: original + one retry = 2 failures, then it stays failed.
    s, dur = chain_topology()
    s.ponds["p2"].retry_on_change = 1
    s.refreshes["p1"] = [continuous("p1")]
    d = Driver(s, dur)
    d.fail_next("s1", 9)
    d.refresh("p2")
    d.run(40)
    p2 = d.state.pond_states["p2"]
    assert p2.is_failed and p2.runs_completed == 0
    assert p2.failures == 2  # no further retries once the budget is spent


@pytest.mark.timeout(1)
def test_blocked_pond_drains_available_output_without_soliciting():
    # p1 produced F1, then failed on a later generation. A blocked p2 may still consume F1 — it just
    # won't re-arm p1 for anything fresher.
    s, _ = chain_topology()
    f1 = T0 + secs(5)
    s.pond_states["p1"].start_f = T0 + secs(6)
    s.pond_states["p1"].end_f = f1
    s.pond_states["p1"].is_failed = True
    s.pond_states["p1"].failed_f = T0 + secs(6)
    s.pond_states["p1"].failures = 1
    derive_blocked(s, "p1")
    assert s.pond_states["p1"].is_blocked and s.pond_states["p2"].is_blocked

    s.pond_states["p2"].demand = [f1]
    out, started = sentinel(T0 + secs(10), s)
    assert out.pond_states["p2"].start_f == f1  # drained the available generation
    assert "s1" in started
    assert not out.pond_states["p1"].demand  # never solicited the failed Source


@pytest.mark.timeout(5)
def test_block_propagates_downstream_and_clears():
    # p1's leaf keeps failing → p1 failed, and p2 blocks by deriving from its failed Source. Clearing
    # p1 unblocks p2 on its own.
    s, dur = chain_topology()
    d = Driver(s, dur)
    d.fail_next("r3", 9)
    d.refresh("p2")
    d.run(15)
    p1, p2 = d.state.pond_states["p1"], d.state.pond_states["p2"]
    assert p1.is_failed and p1.is_blocked
    assert p2.is_blocked and not p2.is_failed  # blocked, not failed — it produced no failure itself
    assert p2.runs_completed == 0

    d.state = clear_pond(d.state, "p1", d.now)
    d._react()
    assert not d.state.pond_states["p1"].is_blocked
    assert not d.state.pond_states["p2"].is_blocked


@pytest.mark.timeout(1)
def test_blocked_pond_ignores_new_demand():
    s, _ = chain_topology()
    for f in ("is_failed", "is_blocked"):
        setattr(s.pond_states["p2"], f, True)
    s.pond_states["p2"].failed_f = T0 + secs(2)
    s.pond_states["p2"].failures = 1
    assert refresh_now(s, "p2", T0).pond_states["p2"].demand == []  # demand ignored


@pytest.mark.timeout(1)
def test_fail_pond_attributes_to_start_f_and_blocks():
    # A whole-Pond failure (dead Duck / Duck-level error) pins failedF to the most recently started
    # Run (startF), stops its Ripples, and blocks downstream.
    s, _ = chain_topology()
    s.pond_states["p1"].start_f = T0 + secs(5)
    s.pond_states["p1"].end_f = T0 + secs(2)
    s.ripple_states["r1"].is_running = True
    out = fail_pond(s, "p1", T0 + secs(10))
    p1 = out.pond_states["p1"]
    assert p1.is_failed and p1.failed_f == T0 + secs(5) and p1.failures == 1
    assert not out.ripple_states["r1"].is_running
    assert out.pond_states["p2"].is_blocked  # downstream derives the block

    # No-op when nothing is in flight (the latest Run already completed).
    s2, _ = chain_topology()
    s2.pond_states["p1"].start_f = s2.pond_states["p1"].end_f = T0 + secs(3)
    assert not fail_pond(s2, "p1", T0 + secs(9)).pond_states["p1"].is_failed


@pytest.mark.timeout(5)
def test_wake_clears_failure_and_runs():
    # Wake is local demand: it clears the failure and runs once because fresh input is available (p1
    # produced T0+1s, p2 never ran), without soliciting p1.
    s, dur = chain_topology()
    s.pond_states["p1"].start_f = s.pond_states["p1"].end_f = T0 + secs(1)  # p1 has output to consume
    ps = s.pond_states["p2"]
    ps.is_failed = ps.is_blocked = True
    ps.failed_f = T0 + secs(2)
    ps.failures = 1
    d = Driver(s, dur)
    d.state = wake_pond(d.state, "p2", d.now)
    assert not d.state.pond_states["p1"].demand  # local: p1 not solicited
    d._react()
    d.run(10)
    p2 = d.state.pond_states["p2"]
    assert not p2.is_failed and not p2.is_blocked
    assert p2.runs_completed >= 1


@pytest.mark.timeout(5)
def test_wake_no_op_when_current():
    # Nothing fresher than the last Run → Wake asks for nothing and runs nothing.
    s, dur = chain_topology()
    f = T0 + secs(1)
    s.pond_states["p1"].start_f = s.pond_states["p1"].end_f = f
    s.pond_states["p2"].start_f = s.pond_states["p2"].end_f = f  # p2 already current with p1
    d = Driver(s, dur)
    d.state = wake_pond(d.state, "p2", d.now)
    d.run(5)
    assert d.state.pond_states["p2"].runs_completed == 0  # sourceF not > startF → no run


@pytest.mark.timeout(5)
def test_force_recomputes_without_advancing_freshness():
    # A fully-current Pond: Force re-runs all Ripples at the same freshness and does NOT propagate
    # downstream (endF returns unchanged).
    s, dur = chain_topology()
    f = T0 + secs(1)
    s.pond_states["p1"].start_f = s.pond_states["p1"].end_f = f
    s.pond_states["p2"].start_f = s.pond_states["p2"].end_f = f
    s.ripple_states["s1"].start_f = s.ripple_states["s1"].end_f = f
    d = Driver(s, dur)
    before = d.state.pond_states["p2"].runs_completed
    d.state = force_pond(d.state, "p2", d.now)
    cmds = [c for c in d.state.pending_begin_runs]
    d._react()
    d.run(5)
    p2 = d.state.pond_states["p2"]
    assert p2.runs_completed == before + 1  # it re-ran
    assert p2.end_f == f  # freshness unchanged → downstream sees no change
    # the dispatched Run carried the force flag
    assert any(c.pond_id == "p2" and c.force for c in cmds) or any(
        c.pond_id == "p2" and c.force for c in d.state.pending_begin_runs
    )


@pytest.mark.timeout(1)
def test_kill_parks_terminal_and_blocks_downstream():
    s, _ = chain_topology()
    # p1 has a Run in flight; Kill cancels it.
    s.pond_states["p1"].start_f = T0 + secs(5)
    s.pond_states["p1"].end_f = T0 + secs(2)
    s.ripple_states["r1"].is_running = True
    out = kill_pond(s, "p1", T0 + secs(6))
    assert out.pond_states["p1"].is_killed
    assert not out.ripple_states["r1"].is_running
    assert out.pond_states["p2"].is_blocked  # downstream blocked by the killed Source
    # Killed supersedes demand: a fresh Refresh does nothing until cleared.
    out2, _ = sentinel(T0 + secs(7), refresh_now(out, "p1", T0 + secs(7)))
    assert out2.pond_states["p1"].runs_started == s.pond_states["p1"].runs_started  # no new run
    # Clear lifts it and unblocks downstream.
    cleared = clear_pond(out, "p1", T0 + secs(8))
    assert not cleared.pond_states["p1"].is_killed and not cleared.pond_states["p2"].is_blocked


# ─── Demand propagation (plans/pulse-throttle.md rules 6 to 8) ─────────────────


@pytest.mark.timeout(1)
def test_demand_sources_are_required_plus_an_unbuilt_optional_or_all():
    s, _ = graph([("A", 1, []), ("B", 1, []), ("C", 1, ["A", "B"])])
    assert demand_sources(s, "C") == ["A", "B"]
    s.ponds["C"].optional_sources = {"B"}
    assert demand_sources(s, "C") == ["A", "B"]  # B has never run: asked, to build its first output
    s.pond_states["B"].demand = [T0]
    assert demand_sources(s, "C") == ["A"]  # already asked
    s.pond_states["B"].demand = []
    s.pond_states["B"].start_f = T0
    assert demand_sources(s, "C") == ["A"]  # built: an optional Source is never waited for
    s.ponds["C"].optional_sources = {"A", "B"}
    assert demand_sources(s, "C") == ["A", "B"]  # only optional: the Pond runs on whichever is fresher


@pytest.mark.timeout(5)
def test_refreshes_build_an_optional_source_once():
    # C requires the 1 s A and takes the 4 s B optionally.
    s, dur = graph([("A", 1, []), ("B", 4, []), ("C", 1, ["A", "B"])])
    s.ponds["C"].optional_sources = {"B"}
    d = Driver(s, dur)
    for _ in range(30):
        d.refresh("C")
        d.run(1)
    d.run(5)
    assert d.state.pond_states["B"].runs_completed == 1  # asked once, to build its first output
    assert d.state.pond_states["C"].runs_completed >= 25  # paced by A, never by B
    d.refresh("B")
    d.run(10)
    assert d.state.pond_states["B"].runs_completed == 2  # B refreshes through its own Refresh


@pytest.mark.timeout(5)
def test_refreshes_reach_every_source_when_all_are_optional():
    s, dur = graph([("A", 1, []), ("B", 1, []), ("C", 1, ["A", "B"])])
    s.ponds["C"].optional_sources = {"A", "B"}
    d = Driver(s, dur)
    for _ in range(2):
        d.refresh("C")
        d.run(5)
    assert d.state.pond_states["A"].runs_completed == 2
    assert d.state.pond_states["B"].runs_completed == 2


@pytest.mark.timeout(5)
def test_repeat_refresh_for_the_running_epoch_neither_reruns_nor_passes_it():
    """Rule 6: demand a Run in flight covers counts as met. A repeat request for the epoch O is running
    (a duct re-sending the downstream's epoch, or a throttle release in the same instant) used to start a
    second Run at the same freshness, which the no-change skip then completed as a pass, ending the
    in-flight Run early."""
    s, dur = chain(("I", 1), ("O", 2))
    d = Driver(s, dur)
    d.refresh("O")
    d.run(1.5)  # I done; O running at freshness T0
    assert d.state.ripple_states["O.r0"].is_running
    d.state = refresh_now(d.state, "O", T0)  # the same epoch again
    d._react()
    o = d.state.pond_states["O"]
    assert o.runs_started == 1 and d.state.pending_passes == []
    assert d.state.ripple_states["O.r0"].is_running  # the in-flight Run is not completed early
    d.run(5)
    assert d.state.pond_states["O"].runs_completed == 1 and d.state.pond_states["O"].end_f == T0


@pytest.mark.timeout(5)
@pytest.mark.parametrize("halted", ["I", "M"])
def test_demand_lost_at_a_halted_source_is_resent_when_it_unblocks(halted):
    """Rule 8. A Run halted mid-flight drops the demand it was serving (cleared at its start), and Clear
    returns it to idle without running. Each Pond that unblocks re-sends its newest unmet demand, so
    the demand reaches the halted Source again (the Inlet, when it was the one halted) and the Outlet is
    served. Without the re-send the whole chain strands."""
    s, dur = chain(("I", 1), ("M", 1), ("N", 1), ("O", 1))
    d = Driver(s, dur)
    d.refresh("O")
    d.run(0.5 if halted == "I" else 1.5)
    assert d.state.ripple_states[f"{halted}.r0"].is_running
    d.state = fail_pond(d.state, halted, d.now)
    del d.inflight[f"{halted}.r0"]  # its Duck died with it
    assert d.state.pond_states["O"].is_blocked
    d.state = clear_pond(d.state, halted, d.now)
    d._react()
    d.run(10)
    for pid in ("I", "M", "N", "O"):
        assert d.state.pond_states[pid].runs_completed == 1, pid
    assert d.state.pond_states["O"].end_f == T0


# ─── Throttle (plans/pulse-throttle.md) ───────────────────────────────────────
# Ported from the playground's playground/tests/unit/throttle.spec.ts, the reference implementation. Under
# saturated demand the bottleneck runs as often as it did under a Wave, upstream settles at the bottleneck's
# rate, and the data is at least as fresh as the Wave's. Sparse demand runs every Pond once per Refresh. Until
# the bottleneck has started its first run nothing can be detected, so the startup runs unpaced; data age is
# therefore compared over the second half, as upstream run counts are. (The startup matters for age here and
# not in the playground: a user's Refresh that reaches a busy Inlet keeps its minted epoch, which the
# playground doesn't model.)
#
# The engine had a pull trigger, the Wave, until Refresh replaced it. WAVE holds what it measured on each
# topology (this simulation, 120 s; Outlet, Inlet and bottleneck runs, and mean age), as the yardstick.

Spec = list[tuple[str, float | list[float], list[str]]]


def _chain_spec(*spec: tuple[str, float | list[float]]) -> Spec:
    return [(name, sec, [spec[i - 1][0]] if i else []) for i, (name, sec) in enumerate(spec)]


# name -> (spec, Outlet, an Inlet whose rate matters, the bottleneck Pond)
THROTTLE_TOPOLOGIES: dict[str, tuple[Spec, str, str, str]] = {
    "demo": ([("transactions", 1, []), ("products", 2, []), ("sales", 2, ["transactions", "products"]),
              ("reports", 1, ["sales"])], "reports", "transactions", "sales"),
    "deep chain": (_chain_spec(("I", 1), ("P1", 1), ("P2", 1), ("P3", 1), ("P4", 1), ("B", 5), ("O", 1)), "O", "I", "B"),
    "long lead": (_chain_spec(("I", 1), ("P1", 1), ("P2", 1), ("P3", 1), ("P4", 1), ("P5", 1), ("P6", 1), ("B", 3),
                              ("O", 1)), "O", "I", "B"),
    "decreasing chain": (_chain_spec(("A", 1), ("B", 3), ("C", 2), ("D", 1)), "D", "A", "B"),
    "slow sibling Inlet": ([("F", 1, []), ("S", 6, []), ("X", 1, ["F", "S"]), ("O", 1, ["X"])], "O", "F", "S"),
    "diamond": ([("A", 1, []), ("B", 5, ["A"]), ("D", 1, ["A"]), ("C", 1, ["B", "D"])], "C", "A", "B"),
    "diamond, long lead on the slow path": (
        [("A", 1, []), ("L1", 1, ["A"]), ("L2", 1, ["L1"]), ("L3", 1, ["L2"]), ("L4", 1, ["L3"]), ("L5", 1, ["L4"]),
         ("B", 3, ["L5"]), ("D", 1, ["A"]), ("C", 1, ["B", "D"])], "C", "D", "B"),
    "Inlet with Ripples in sequence": (_chain_spec(("I", [1, 2, 1.5]), ("O", 1)), "O", "I", "I"),
    "Outlet is the bottleneck": (_chain_spec(("I", 1), ("O", 4)), "O", "I", "O"),
    "Outlet is the bottleneck behind a long lead": (
        _chain_spec(("I", 1), ("P1", 1), ("P2", 1), ("P3", 1), ("P4", 1), ("P5", 1), ("O", 4)), "O", "I", "O"),
    "bottleneck inside a Pond": (_chain_spec(("I", 1), ("P", [1, 4, 1]), ("O", 1)), "O", "I", "P"),
}
WAVE: dict[str, dict[str, tuple[dict[str, int], float]]] = {
    "demo": {
        "wave": ({"reports": 38, "transactions": 41, "sales": 38}, 10.0),
        "wave_drift": ({"reports": 41, "transactions": 44, "sales": 41}, 10.2378),
    },
    "deep chain": {
        "wave": ({"O": 22, "I": 28, "B": 22}, 31.0),
        "wave_drift": ({"O": 24, "I": 30, "B": 24}, 31.5025),
    },
    "long lead": {
        "wave": ({"O": 37, "I": 45, "B": 37}, 25.0),
        "wave_drift": ({"O": 40, "I": 48, "B": 40}, 25.5173),
    },
    "decreasing chain": {
        "wave": ({"D": 38, "A": 41, "B": 39}, 9.0),
        "wave_drift": ({"D": 41, "A": 44, "B": 42}, 9.1988),
    },
    "slow sibling Inlet": {
        "wave": ({"O": 19, "F": 20, "S": 19}, 8.0),
        "wave_drift": ({"O": 21, "F": 22, "S": 21}, 8.2089),
    },
    "diamond": {
        "wave": ({"C": 23, "A": 25, "B": 23}, 11.0),
        "wave_drift": ({"C": 25, "A": 27, "B": 25}, 11.1607),
    },
    "diamond, long lead on the slow path": {
        "wave": ({"C": 37, "D": 38, "B": 37}, 22.0),
        "wave_drift": ({"C": 40, "D": 41, "B": 40}, 22.3384),
    },
    "Inlet with Ripples in sequence": {
        "wave": ({"O": 58, "I": 58}, 6.5),
        "wave_drift": ({"O": 61, "I": 62}, 6.7288),
    },
    "Outlet is the bottleneck": {
        "wave": ({"O": 29, "I": 31}, 8.0),
        "wave_drift": ({"O": 32, "I": 33}, 8.0872),
    },
    "Outlet is the bottleneck behind a long lead": {
        "wave": ({"O": 28, "I": 35}, 28.0),
        "wave_drift": ({"O": 30, "I": 37}, 28.3932),
    },
    "bottleneck inside a Pond": {
        "wave": ({"O": 28, "I": 32, "P": 29}, 14.0),
        "wave_drift": ({"O": 31, "I": 34, "P": 31}, 14.3099),
    },
}
# Beyond the playground spec's topologies: only cross-checked, not part of the saturation port.
CROSSCHECK_ONLY = {
    "two Outlets on a shared chain": ([("I", 1, []), ("S", 3, ["I"]), ("O1", 1, ["S"]), ("O2", 1, ["S"])], "O1", "I", "S"),
}


def _throttle_graph(name: str) -> tuple[EngineState, dict[str, timedelta]]:
    s, dur = graph(THROTTLE_TOPOLOGIES[name][0])
    if name == "demo":  # the playground's demo: `sales.join` (3 s) waits on `daily` (2 s) and `tiers` (1 s)
        for rid, rname, parents, d in (("sales.r1", "tiers", [], 1), ("sales.r2", "join", ["sales.r0", "sales.r1"], 3)):
            s.ripples[rid] = Ripple(rid, "sales", rname, parents=parents)
            s.ripple_states[rid] = RippleState()
            dur[rid] = secs(d)
    return s, dur


@dataclass
class SimRun:
    runs: dict[str, int]  # completed Pond Runs
    late: dict[str, int]  # completed Pond Runs in the second half only
    age: float  # mean data age (now - freshness) at the Outlet's completions in the second half, seconds
    passes: int
    completions: dict[str, list[datetime]]


def simulate(made: tuple[EngineState, dict[str, timedelta]], outlet: str, seconds: int, *, every: float | None = None,
             tide: float | None = None, drift: bool = False, tides: dict[str, float] | None = None,
             schedules: dict[str, tuple[float, float]] | None = None) -> SimRun:
    """Drive sim time as the playground's tests do: a one-off Refresh at ``outlet`` every ``every`` seconds
    (landing on the next step), or a standing floating Refresh every ``tide`` seconds (0 = continuous; the
    name follows the cross-check's scenario format); then per step advance, complete, tick, react."""
    s, dur = made
    if tide is not None:
        s.refreshes = {outlet: [floating(outlet, tide)]}
    for pond, bound in (tides or {}).items():  # further Outlets with their own cadences
        s.refreshes[pond] = [floating(pond, bound)]
    for pond, (at, period) in (schedules or {}).items():  # scheduled Refreshes, on a grid from the start
        s.refreshes[pond] = [*s.refreshes.get(pond, []), scheduled(pond, T0 + secs(at), period)]
    d = Driver(s, dur, drift=drift)
    ages: list[float] = []
    last_end, half = NEVER, {}
    for i in range(seconds * 10):
        if i == seconds * 5:
            half = {pid: ps.runs_completed for pid, ps in d.state.pond_states.items()}
        if every and i % round(every * 10) == 0:
            d.state = refresh_now(d.state, outlet, d.now)
        d.advance()
        d.process()
        end = d.state.pond_states[outlet].end_f
        if end != last_end:
            last_end = end
            if i >= seconds * 5:
                ages.append((d.now - end).total_seconds())
    runs = {pid: ps.runs_completed for pid, ps in d.state.pond_states.items()}
    return SimRun(
        runs=runs,
        late={pid: n - half.get(pid, 0) for pid, n in runs.items()},
        age=sum(ages) / max(1, len(ages)),
        passes=len(d.state.pending_passes),
        completions={pid: ps.completion_times for pid, ps in d.state.pond_states.items()},
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize("name", list(THROTTLE_TOPOLOGIES))
def test_saturated_demand_runs_at_the_bottlenecks_pace(name):
    _, outlet, inlet, bottleneck = THROTTLE_TOPOLOGIES[name]
    wave, wave_drift = WAVE[name]["wave"], WAVE[name]["wave_drift"]
    for opts, (ref_runs, ref_age) in ((dict(every=1), wave), (dict(tide=0), wave), (dict(tide=1, drift=True), wave_drift)):
        r = simulate(_throttle_graph(name), outlet, 120, **opts)
        assert r.runs[bottleneck] == ref_runs[bottleneck], opts
        assert r.runs[outlet] == ref_runs[outlet], opts
        assert r.late[inlet] <= r.late[bottleneck] + 1, opts  # settled: one per bottleneck run
        assert r.runs[inlet] <= wave[0][inlet] + 14, opts  # the startup, before detection, plus top-ups
        assert r.age <= ref_age + 0.1, opts
        assert r.passes == 0, opts  # rule 6: no repeat request completes a Run early as a pass


@pytest.mark.timeout(10)
@pytest.mark.parametrize("name", list(THROTTLE_TOPOLOGIES))
def test_sparse_refreshes_run_every_pond_once_per_refresh(name):
    _, outlet, _, _ = THROTTLE_TOPOLOGIES[name]
    r = simulate(_throttle_graph(name), outlet, 120, every=60)
    assert r.runs == {pid: 2 for pid in r.runs}


@pytest.mark.timeout(5)
def test_an_inlet_stamps_a_release_at_its_start_and_a_refresh_at_its_epoch():
    """A throttle release carries no epoch anyone asked for, so a busy Inlet it reaches stamps its next Run's
    start. A user's Refresh keeps its minted epoch however late the Inlet gets to it."""
    for release in (False, True):
        d = Driver(*chain(("I", 4), ("O", 1)))
        d.refresh("O")
        d.run(1)  # I busy until T0+4
        t = d.now
        add_demand(d.state, "I", t, release=release)
        d.run(4)  # I's first Ripple frees at T0+4 and the next Run starts
        assert d.state.pond_states["I"].start_f == (T0 + secs(4) if release else t)


@pytest.mark.timeout(10)
def test_a_slow_optional_source_neither_throttles_its_sink_nor_is_asked_again():
    # C requires the 1 s A and takes the 4 s B optionally.
    s, dur = graph([("A", 1, []), ("B", 4, []), ("C", 1, ["A", "B"])])
    s.ponds["C"].optional_sources = {"B"}
    r = simulate((s, dur), "C", 120, every=1)
    assert r.runs["B"] == 1  # asked once, to build its first output; never again
    assert r.runs["C"] > 50  # paced by A (and its own 1 s run), never by B

    # Slow under its own continuous Refresh, B is throttled, and the flag still doesn't cross the optional edge.
    s, dur = graph([("A", 1, []), ("B", 4, []), ("C", 1, ["A", "B"])])
    s.ponds["C"].optional_sources = {"B"}
    s.refreshes = {"B": [continuous("B")]}
    d = Driver(s, dur)
    for _ in range(30):
        d.refresh("C")
        d.run(1)
    assert is_throttled(d.state, "B") and not is_throttled(d.state, "C")
    assert d.state.pond_states["C"].runs_completed >= 25


@pytest.mark.timeout(10)
def test_a_pond_with_only_optional_sources_is_throttled_only_when_all_of_them_are():
    s, dur = graph([("A", 1, []), ("B", 4, []), ("C", 1, ["A", "B"])])
    s.ponds["C"].optional_sources = {"A", "B"}
    r = simulate((s, dur), "C", 120, every=1)
    assert r.runs["A"] > 50  # B is slow, A isn't, so C isn't throttled and A keeps up
    assert r.runs["C"] > 50


def _saturated_deep_chain(seconds: int = 30) -> Driver:
    """The deep chain under a Refresh every second, long enough for B (5 s) to be the detected bottleneck."""
    d = Driver(*_throttle_graph("deep chain"))
    for _ in range(seconds):
        d.refresh("O")
        d.run(1)
    assert d.state.pond_states["B"].throttle.slow == {"B.r0"}
    assert is_throttled(d.state, "O") and d.state.pond_states["O"].throttle.trigger
    return d


@pytest.mark.timeout(5)
def test_sleep_kill_and_clear_drop_throttle_state():
    d = _saturated_deep_chain()
    slept = sleep_pond(d.state, "O", d.now, upstream=True)
    assert all(ps.throttle is None for ps in slept.pond_states.values())
    killed = kill_pond(d.state, "B", d.now)
    assert killed.pond_states["B"].throttle is None and not is_throttled(killed, "O")
    cleared = clear_pond(d.state, "B", d.now)
    assert cleared.pond_states["B"].throttle is None


@pytest.mark.timeout(5)
def test_force_is_neither_counted_nor_held_and_a_failed_bottleneck_clears():
    d = _saturated_deep_chain()
    for pid in ("B", "O"):
        forced = force_pond(d.state, pid, d.now)
        assert forced.pond_states[pid].throttle == d.state.pond_states[pid].throttle

    failed = fail_pond(d.state, "B", d.now)
    failed, _ = sentinel(d.now, failed)
    assert not failed.pond_states["B"].throttle.slow  # its Ripple isn't running: slow clears
    assert not is_throttled(failed, "O")


@pytest.mark.timeout(5)
def test_a_repairing_pond_neither_counts_nor_holds():
    for repairing in (False, True):
        d = Driver(*chain(("I", 1), ("B", 5), ("O", 1)))
        d.refresh("O")
        d.run(1.5)  # I done; B running
        d.state.pond_states["B"].repairing = repairing
        t1, t2 = d.now, d.now + STEP
        add_demand(d.state, "B", t1)
        add_demand(d.state, "B", t2)  # the second demand in one of B's runs
        th = d.state.pond_states["B"].throttle
        if repairing:
            assert not th.slow and d.state.pond_states["I"].demand == [t1, t2]
        else:
            assert th.slow == {"B.r0"} and th.held == 1 and d.state.pond_states["I"].demand == [t1]


@pytest.mark.timeout(5)
def test_a_pass_counts_as_a_trigger_pond_completion():
    """A throttled trigger Pond releases held demand on its completions, and a no-change pass is one."""
    t1, t2, t3, now = (T0 + secs(n) for n in (1, 2, 3, 4))
    for passes in (False, True):
        s, _ = chain(("I", 1), ("B", 5), ("O", 1))
        s.pond_states["I"].start_f = s.pond_states["I"].end_f = s.pond_states["I"].changed_f = t2
        b = s.pond_states["B"]  # the bottleneck, slow, running at t2; its last Run (t1) changed nothing
        b.start_f, b.end_f, b.changed_f = t2, t1, T0
        s.ripple_states["B.r0"] = RippleState(start_f=t2, end_f=t1, is_running=True, runs_started=3)
        b.throttle = ThrottleState(slow={"B.r0"}, sent_at={"B.r0": 3})
        o = s.pond_states["O"]  # the Outlet, idle at T0, holding a request at t3
        o.start_f = o.end_f = o.changed_f = T0
        o.demand = [t1, t3] if passes else [t3]
        o.throttle = ThrottleState(trigger=True, held=1, completed=False)  # a request is outstanding

        s, _ = sentinel(now, s)
        if passes:  # O ran at t1 as a pass (B's output unchanged), which is its turn to release
            assert s.pending_passes == [("O", t1)]
            assert s.pond_states["O"].throttle.held == 0 and now in s.pond_states["B"].demand
        else:
            assert s.pond_states["O"].throttle.held == 1 and s.pond_states["B"].demand == []


@pytest.mark.timeout(10)
def test_a_join_aligns_a_fast_source_with_a_throttled_one():
    """Sibling alignment: S (6 s) is throttled and F (1 s) isn't, so a request reaches S at once and F only when
    S starts its next Run. The two read together, and the join's freshness isn't the older of two stamps (before
    this, F read early and the stamps sat 2 s apart)."""
    s, dur = graph([("F", 1, []), ("S", 6, []), ("X", 1, ["F", "S"]), ("O", 1, ["X"])])
    s.refreshes = {"O": [continuous("O")]}
    d = Driver(s, dur)
    seen, gaps = 0, []
    for i in range(1200):
        d.step()
        if d.state.pond_states["X"].runs_started != seen:
            seen = d.state.pond_states["X"].runs_started
            if i > 600:
                gaps.append(abs(d.state.pond_states["F"].end_f - d.state.pond_states["S"].end_f))
    assert gaps and max(gaps) == timedelta(0)
    assert d.state.pond_states["O"].runs_completed >= 18  # still paced by S: one run per 6 s


@pytest.mark.timeout(5)
def test_a_held_back_source_that_becomes_throttled_gets_its_demand_at_once():
    """Demand held back from F (while S is the throttled Source) goes to F as soon as F is throttled itself.
    Releasing only to Sources not throttled at that moment once lost it (a random-graph fuzz found 68 requests
    stranded that way)."""
    s, dur = graph([("F", 1, []), ("S", 6, []), ("X", 1, ["F", "S"])])
    for src in ("F", "S"):
        s.ripple_states[f"{src}.r0"] = RippleState(start_f=T0, is_running=True, runs_started=1)
        s.pond_states[src].start_f = T0
    s.pond_states["S"].throttle = ThrottleState(slow={"S.r0"})
    t = T0 + secs(1)
    add_demand(s, "X", t)
    assert t in s.pond_states["S"].demand and s.pond_states["F"].demand == []
    assert s.pond_states["X"].throttle.deferred == {"F": (t, False)}
    s.pond_states["F"].throttle = ThrottleState(slow={"F.r0"})  # F becomes throttled before S starts again
    s, _ = sentinel(t, s)
    assert t in s.pond_states["F"].demand and s.pond_states["X"].throttle.deferred == {}


@pytest.mark.timeout(5)
def test_every_counts_from_the_later_of_its_request_and_the_freshness():
    """A floating Refresh set on a Pond refreshed by hand 30 s earlier first fires 60 s after that refresh's
    freshness: it counts from the later of its own newest request and the Pond's freshness, so a Run it didn't
    ask for counts."""
    d = Driver(*chain(("I", 1), ("O", 1)))
    d.refresh("O")  # a manual refresh at T0
    d.run(30)
    d.state.refreshes = {"O": [floating("O", 60)]}
    d.run(65)
    o = d.state.pond_states["O"]
    assert o.runs_completed == 2 and o.end_f == T0 + secs(60)

    # A Run the Refresh didn't ask for also counts: M's own Refresh fires at T0, O's Refresh refreshes M at
    # T0+20, so M's next request is due at T0+80 (counting from its last request alone would make it T0+60).
    d = Driver(*chain(("I", 1), ("M", 1), ("O", 1)))
    d.state.refreshes = {"M": [floating("M", 60)]}
    d.run(20)
    d.refresh("O")
    d.run(65)
    m = d.state.pond_states["M"]
    assert m.runs_completed == 3 and m.end_f == T0 + secs(80)


@pytest.mark.timeout(10)
def test_every_beside_a_busy_neighbour_stretches_by_at_most_one_upstream_run():
    """Beside a continuous neighbour on shared upstream, a request can be served by a later generation; the next
    request then counts from that fresher data, so each cycle stretches by at most one run of S (3 s)."""
    s, dur = graph([("I", 1, []), ("S", 3, ["I"]), ("O1", 1, ["S"]), ("O2", 1, ["S"])])
    s.refreshes = {"O1": [continuous("O1")], "O2": [floating("O2", 10)]}
    d = Driver(s, dur)
    d.run(300)
    late = [t for t in d.state.pond_states["O2"].completion_times if t >= T0 + secs(150)]
    gaps = gaps_seconds(late)
    assert 10 <= sum(gaps) / len(gaps) <= 13


@pytest.mark.timeout(5)
def test_sleeping_a_mid_pipeline_pond_keeps_the_demand_below_it():
    """Sleep clears a Pond's own demand, but a Sink's pending request it was serving is sent to it again, so the
    Sink's refresh still completes instead of stranding."""
    d = Driver(*chain(("I", 1), ("M", 1), ("O", 1)))
    d.refresh("O")
    d.run(0.5)  # I running; M and O waiting for T0
    d.state = sleep_pond(d.state, "M", d.now)
    d._react()
    d.run(10)
    assert d.state.pond_states["O"].end_f == T0 and d.state.pond_states["O"].demand == []


@pytest.mark.timeout(5)
def test_sleeping_one_outlet_upstream_keeps_another_outlets_demand():
    """Sleeping A with its upstream sweeps the shared Source S; B's pending request at S is sent again."""
    d = Driver(*graph([("S", 1, []), ("A", 1, ["S"]), ("B", 1, ["S"])]))
    d.refresh("A")  # S runs for A at T0
    d.run(0.5)
    b_at = d.now
    d.refresh("B")  # S is busy, so B's request waits at S
    assert b_at in d.state.pond_states["S"].demand
    d.state = sleep_pond(d.state, "A", d.now, upstream=True)
    d._react()
    d.run(10)
    assert d.state.pond_states["B"].end_f == b_at
    assert d.state.pond_states["A"].runs_completed == 0  # A was still waiting on S: its demand is gone


_PLAYGROUND_ENGINE = Path(__file__).parent.parent / "playground" / "src" / "lib" / "orchestration.ts"


def _node_runs_typescript() -> bool:
    node = shutil.which("node")
    if node is None or not _PLAYGROUND_ENGINE.exists():
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(x) for x in out.split(".")[:2])
    return (major, minor) >= (22, 6)  # --experimental-strip-types


@pytest.mark.timeout(30)
@pytest.mark.skipif(not _node_runs_typescript(), reason="needs Node 22.6+ and the playground source")
@pytest.mark.parametrize("name,opts", [
    ("deep chain", dict(every=1)),
    ("diamond", dict(tide=1, drift=True)),
    ("Outlet is the bottleneck", dict(tide=0)),
    ("bottleneck inside a Pond", dict(every=1)),
    ("slow sibling Inlet", dict(tide=0)),  # sibling alignment at the join
    ("two Outlets on a shared chain", dict(tide=0, tides={"O2": 10})),  # every 10 s beside a continuous neighbour
    ("deep chain", dict(schedules={"O": [3, 7]})),  # a scheduled Refresh
])
def test_the_throttle_matches_the_playground_engine(name, opts):
    """Cross-check against the reference: the same scenario through the playground's TypeScript engine and
    this one gives the same Pond Runs at the same times. (They can differ where a user's Refresh reaches a busy
    Inlet, which stamps the Refresh's minted epoch here and its start time in the playground; a scheduled Refresh
    beside another one does, since the stamp moves the other's clock.)"""
    spec, outlet, _, _ = THROTTLE_TOPOLOGIES.get(name) or CROSSCHECK_ONLY[name]
    scenario = {"spec": spec, "outlet": outlet, "seconds": 120, **opts}
    out = subprocess.run(
        ["node", "--experimental-strip-types", str(Path(__file__).parent / "throttle_crosscheck.mts"),
         json.dumps(scenario)],
        capture_output=True, text=True, check=True,
    )
    ts = json.loads(out.stdout)
    py = simulate(_throttle_graph(name) if name in THROTTLE_TOPOLOGIES else graph(spec), outlet, 120, **opts)
    assert py.runs == ts["runs"]
    ms = {pid: [round((t - T0) / timedelta(milliseconds=1)) for t in times] for pid, times in py.completions.items()}
    assert ms == ts["completions"]


# ─── No-change skip: content freshness (changed_f) and the Pond pass ───────────
# plans/no-change-skip.md. A Pond with Sources whose content is unchanged since it last ran is
# completed in-engine as a *pass* (no BeginRun): freshness/heartbeat advance, changed_f is held.


def _settle_chain_at(s: EngineState, f: datetime) -> None:
    """Put the p1 -> p2 chain in a clean idle state, both Ponds run and changed at freshness ``f``."""
    for pid in ("p1", "p2"):
        ps = s.pond_states[pid]
        ps.start_f = ps.end_f = ps.changed_f = f
    for rid in ("r1", "r2", "r3", "s1"):
        s.ripple_states[rid].start_f = s.ripple_states[rid].end_f = f


@pytest.mark.timeout(1)
def test_unchanged_source_makes_sink_pass_without_dispatch():
    s, _ = chain_topology()
    f1 = T0 + secs(5)
    _settle_chain_at(s, f1)
    # p1 republishes at f2 with NO content change (changed_f stays f1); its ripples advance.
    f2 = T0 + secs(10)
    s.pond_states["p1"].start_f = s.pond_states["p1"].end_f = f2
    for rid in ("r1", "r2", "r3"):
        s.ripple_states[rid].start_f = s.ripple_states[rid].end_f = f2
    s.pond_states["p2"].demand = [f2]  # p2 is asked for fresher data

    out, started = sentinel(f2, s)
    p2 = out.pond_states["p2"]
    assert "s1" not in started  # no Ripple ran — the Pond passed
    assert not out.pending_begin_runs  # and no Duck was dispatched
    assert p2.end_f == f2  # freshness (the heartbeat) advanced
    assert p2.changed_f == f1  # but the content mark is held
    assert p2.runs_completed == s.pond_states["p2"].runs_completed + 1  # the pass is a completed Run


@pytest.mark.timeout(1)
def test_changed_source_makes_sink_do_real_work():
    s, _ = chain_topology()
    f1 = T0 + secs(5)
    _settle_chain_at(s, f1)
    f2 = T0 + secs(10)
    s.pond_states["p1"].start_f = s.pond_states["p1"].end_f = s.pond_states["p1"].changed_f = f2
    for rid in ("r1", "r2", "r3"):
        s.ripple_states[rid].start_f = s.ripple_states[rid].end_f = f2
    s.pond_states["p2"].demand = [f2]

    out, started = sentinel(f2, s)
    assert "s1" in started  # p1 changed → p2 does real work
    assert any(br.pond_id == "p2" for br in out.pending_begin_runs)  # dispatched to a Duck


@pytest.mark.timeout(1)
def test_skip_compares_prior_freshness_not_new_startf():
    """The operand correctness case (plans/no-change-skip.md). X reads A and B. A: endF 8 / changedF 6;
    B: endF 10 / changedF 7; X last ran at priorF 5. B changed at 7 — newer than X's prior freshness —
    so X must do real work, even though 7 < X's new startF (min(8,10)=8). The wrong ``>= startF`` rule
    would pass here and miss B's change."""
    s, _ = diamond_topology()
    s.pond_states["A"].start_f = s.pond_states["A"].end_f = T0 + secs(8)
    s.pond_states["A"].changed_f = T0 + secs(6)
    s.pond_states["B"].start_f = s.pond_states["B"].end_f = T0 + secs(10)
    s.pond_states["B"].changed_f = T0 + secs(7)
    s.pond_states["X"].start_f = s.pond_states["X"].end_f = s.pond_states["X"].changed_f = T0 + secs(5)
    s.pond_states["X"].demand = [T0 + secs(8)]

    out, started = sentinel(T0 + secs(12), s)
    assert out.pond_states["X"].start_f == T0 + secs(8)  # ran at min(8, 10)
    assert "x" in started  # real work — B's change at 7 (> priorF 5) is incorporated
    assert any(br.pond_id == "X" for br in out.pending_begin_runs)


@pytest.mark.timeout(1)
def test_always_run_pond_dispatches_despite_unchanged_sources():
    s, _ = chain_topology()
    s.ponds["p2"].always_run = True  # a side-effecting Pond must run every time
    f1 = T0 + secs(5)
    _settle_chain_at(s, f1)
    f2 = T0 + secs(10)
    s.pond_states["p1"].start_f = s.pond_states["p1"].end_f = f2  # republish, changed_f held at f1
    for rid in ("r1", "r2", "r3"):
        s.ripple_states[rid].start_f = s.ripple_states[rid].end_f = f2
    s.pond_states["p2"].demand = [f2]

    out, started = sentinel(f2, s)
    assert "s1" in started  # always_run bypasses the pass
    assert any(br.pond_id == "p2" for br in out.pending_begin_runs)


@pytest.mark.timeout(5)
def test_continuous_steady_state_only_inlet_runs_when_nothing_changes():
    """Under a continuous Refresh, an Inlet that stops changing leaves the whole interior quiet: the Inlet
    keeps polling (real runs), every Sink passes (freshness climbs, no Ripple work)."""
    s, dur = chain_topology()
    s.refreshes["p2"] = [continuous("p2")]
    d = Driver(s, dur)
    d.run(10)  # reach steady state
    d.mark_unchanged("r3")  # p1's leaf — p1's output stops changing from here
    d.run(2)  # let one more p1 run land as unchanged
    s1_runs = d.state.ripple_states["s1"].runs_started
    p2_completed = d.state.pond_states["p2"].runs_completed
    p2_changed_f = d.state.pond_states["p2"].changed_f
    d.run(10)
    p1, p2 = d.state.pond_states["p1"], d.state.pond_states["p2"]
    assert p1.end_f > p2_changed_f  # the Inlet kept polling (freshness advanced)
    assert p2.runs_completed > p2_completed  # p2 kept completing — as passes
    assert d.state.ripple_states["s1"].runs_started == s1_runs  # but its Ripple never ran again
    assert p2.changed_f == p2_changed_f  # content mark frozen


# ─── Pool-aware freshness visibility (plans/persist.md phase 3) ────────────────


def test_cross_pool_sink_gates_on_persisted_f_same_pool_on_end_f():
    """The one-rule protocol: a Sink sees its Source at ``end_f`` when they share a Pool (the local
    publish is the handoff), at ``persisted_f`` otherwise (it can only read what the durable mirror has
    landed). So a same-Pool Sink starts as soon as the Source completes; a cross-Pool Sink holds until
    ``record_persist`` — and then starts at the persisted freshness."""
    from duckstring.engine import record_persist

    src = Pond("src", "src", pool="catchment", async_persist=True)
    loc = Pond("loc", "loc", sources=["src"], pool="catchment")   # same Pool → end_f
    rmt = Pond("rmt", "rmt", sources=["src"], pool="duck:rmt")    # cross-Pool → persisted_f
    s = build([src, loc, rmt], [])

    # The Source has completed a run locally, but its mirror has not landed yet.
    s.pond_states["src"].start_f = s.pond_states["src"].end_f = T0
    s = refresh_now(s, "loc", T0)
    s = refresh_now(s, "rmt", T0)
    s = tick(T0, s)
    s, _ = sentinel(T0, s)
    started = [b.pond_id for b in s.pending_begin_runs]
    assert "loc" in started, "a same-Pool Sink must start at the Source's end_f"
    assert "rmt" not in started, "a cross-Pool Sink must NOT start before the Source's mirror lands"
    assert s.pond_states["rmt"].start_f == NEVER

    # The mirror lands → the cross-Pool Sink becomes runnable at the persisted freshness.
    s.pending_begin_runs.clear()
    record_persist(s, "src", T0)
    at = T0 + STEP
    s = tick(at, s)
    s, _ = sentinel(at, s)
    assert "rmt" in [b.pond_id for b in s.pending_begin_runs]
    assert s.pond_states["rmt"].start_f == T0


def test_record_persist_is_monotonic():
    """A replayed/stale persist report never regresses the watermark."""
    from duckstring.engine import record_persist

    src = Pond("src", "src", pool="catchment", async_persist=True)
    s = build([src], [])
    record_persist(s, "src", T0 + secs(10))
    record_persist(s, "src", T0)  # a stale replay
    assert s.pond_states["src"].persisted_f == T0 + secs(10)


def test_non_async_pond_persisted_f_tracks_end_f_on_completion():
    """A Pond whose publish is durable at completion (no cloud / a remote Duck writing the plane
    directly) keeps ``persisted_f == end_f`` — cross-Pool visibility never lags, and the pre-Pool
    behaviour is exactly preserved (source_visible_f returns end_f for it regardless)."""
    src = Pond("src", "src")  # defaults: pool="catchment", async_persist=False
    s = build([src], [Ripple("r", "src", "r")])
    s.pond_states["src"].start_f = T0
    s.ripple_states["r"].start_f = T0
    s.ripple_states["r"].is_running = True
    s = complete_ripple(s, "r", T0)
    assert s.pond_states["src"].end_f == T0
    assert s.pond_states["src"].persisted_f == T0


def test_async_pond_persisted_f_lags_until_recorded():
    """An async_persist Pond's completion advances end_f but NOT persisted_f — the mirror is off the
    critical path, and only record_persist (the Duck's persist event) advances the watermark."""
    src = Pond("src", "src", async_persist=True)
    s = build([src], [Ripple("r", "src", "r")])
    s.pond_states["src"].start_f = T0
    s.ripple_states["r"].start_f = T0
    s.ripple_states["r"].is_running = True
    s = complete_ripple(s, "r", T0)
    assert s.pond_states["src"].end_f == T0
    assert s.pond_states["src"].persisted_f == NEVER
