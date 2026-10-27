"""Memory slots in the engine's start decision (plans/memory-budget.md).

A machine's Ducks share ``N`` slots. A Duck takes its slots when it goes from idle to busy and gives them back
when nothing of its Pond is in flight; a Pond ready to start without one isn't started, and when its turn comes
it starts on its Sources' newest freshness. FIFO, no backfilling, spare slots to a lone Run less one while other
demand is outstanding, and the throttle counts a Pond waiting for a slot as busy.
"""

from __future__ import annotations

import random

import pytest
from test_engine import T0, Driver, chain, continuous, graph, secs

from duckstring.engine import catchment as engine
from duckstring.engine.core import NEVER

GROUP = "catchment"


def slotted(made, n: int):
    s, dur = made
    s.slot_capacity = {GROUP: n}
    return s, dur


def used(state) -> int:
    return sum(ps.slots for pid, ps in state.pond_states.items() if state.ponds[pid].pool == GROUP)


def refresh_together(d, *pids) -> None:
    """Refresh several Ponds in one pass (the Driver's batching): they're admitted together."""
    for pid in pids:
        d.state = engine.refresh_now(d.state, pid, d.now)
    d._react()


class Checked(Driver):
    """The test Driver, checking after every step that the slots in use never exceed the capacity, and
    recording each Pond Run's start (its freshness, and its Sources' freshness then)."""

    def __init__(self, made, n: int):
        super().__init__(*slotted(made, n))
        self.n = n
        self.starts: dict[str, list[tuple]] = {}

    def _react(self) -> None:
        before = {pid: ps.runs_started for pid, ps in self.state.pond_states.items()}
        super()._react()
        assert used(self.state) <= self.n
        for pid, ps in self.state.pond_states.items():
            if ps.runs_started > before[pid]:
                srcs = {sp: self.state.pond_states[sp].end_f for sp in self.state.ponds[pid].sources}
                self.starts.setdefault(pid, []).append((self.now, ps.start_f, srcs, ps.slots))

    def process(self) -> None:
        super().process()
        assert used(self.state) <= self.n


@pytest.mark.timeout(30)
def test_without_slots_configured_scheduling_is_unchanged():
    """Plenty of slots changes nothing: the same Runs at the same times as with no gate."""
    made = graph([("A", 1, []), ("B", 2, []), ("C", [1, 3], ["A", "B"]), ("D", 1, ["C"]), ("E", 2, ["C"])])
    runs = []
    for n in (None, 100):
        s, dur = graph([("A", 1, []), ("B", 2, []), ("C", [1, 3], ["A", "B"]), ("D", 1, ["C"]), ("E", 2, ["C"])])
        if n:
            s.slot_capacity = {GROUP: n}
        s.refreshes = {"D": [continuous("D")], "E": [continuous("E")]}
        d = Driver(s, dur)
        d.run(60)
        runs.append({pid: (ps.runs_completed, ps.completion_times) for pid, ps in d.state.pond_states.items()})
    assert runs[0] == runs[1]
    assert made  # the graph builds


@pytest.mark.timeout(30)
def test_a_waiting_pond_runs_once_on_its_sources_newest_freshness():
    """One slot shared by a fast continuous Inlet and a slow Sink: each time the Sink gets the slot it starts
    on the Inlet's newest output and clears everything it was asked for, so the Inlet Runs it waited
    through are never run by it."""
    d = Checked(chain(("I", 1), ("S", 3)), 1)
    d.state.refreshes = {"S": [continuous("S")]}
    d.run(60)
    s_starts = d.starts["S"]
    assert len(s_starts) >= 5
    for _, f, srcs, _ in s_starts:
        assert f == srcs["I"]  # the Inlet's freshness at the moment the slot came
    fs = [f for _, f, _, _ in s_starts]
    assert fs == sorted(set(fs))  # never the same freshness twice
    assert d.state.pond_states["S"].runs_completed < d.state.pond_states["I"].runs_completed + 1


@pytest.mark.timeout(30)
def test_fifo_gets_data_through_behind_continuous_inlets():
    """The worst case: two continuously refreshed Inlets and a Sink on one slot. FIFO by when each began
    waiting puts the Sink ahead of an Inlet that is ready again, so data reaches the Sink steadily."""
    d = Checked(graph([("A", 1, []), ("B", 1, []), ("C", 2, ["A", "B"])]), 1)
    d.state.refreshes = {"C": [continuous("C")]}
    d.run(120)
    ps = d.state.pond_states["C"]
    assert ps.runs_completed >= 20  # a Sink Run every A, B, C turn (4 s at best, 5 to 6 s in steps)
    assert (d.now - ps.end_f).total_seconds() < 10  # and its data stays young


@pytest.mark.timeout(10)
def test_siblings_share_the_slots_and_a_lone_run_keeps_one_spare():
    d = Checked(graph([("P", 2, []), ("X", 2, ["P"]), ("Y", 2, ["P"]), ("Z", 2, ["P"])]), 4)
    for pid in ("X", "Y", "Z"):
        d.refresh(pid)
    p_slots = d.starts["P"][0][3]
    assert p_slots == 3  # alone, but the Sinks' demand is outstanding: one slot stays free
    d.run(2.5)  # P completes; the three Sinks become ready in the same pass
    times = {d.starts[pid][0][0] for pid in ("X", "Y", "Z")}
    assert len(times) == 1  # admitted together
    assert sorted(d.state.pond_states[pid].slots for pid in ("X", "Y", "Z")) == [1, 1, 1]  # one kept for P
    d.run(3)
    assert used(d.state) == 0


@pytest.mark.timeout(10)
def test_an_exclusive_run_waits_for_the_slots_to_drain_and_holds_the_queue():
    d = Checked(graph([("A", 5, []), ("B", 1, []), ("C", 1, [])]), 4)
    d.refresh("A")  # A takes three slots (others' demand isn't outstanding yet... it is now, below)
    d.run(0.5)
    d.state.pond_states["B"].slot_exclusive = True
    d.refresh("B")  # needs every slot: waits for A
    d.run(0.5)
    d.refresh("C")  # one slot is free, but B is ahead of C and holds the queue
    d.run(1)
    assert d.state.pond_states["B"].runs_started == 0 and d.state.pond_states["C"].runs_started == 0
    d.run(4)  # A completes at 5 s
    assert d.starts["B"][0][3] == 4 and d.state.pond_states["C"].runs_started == 0  # B alone, with everything
    d.run(1.5)
    assert d.state.pond_states["C"].runs_completed == 1  # then C


@pytest.mark.timeout(10)
def test_a_pass_takes_no_slot():
    """A Sink whose Sources didn't change completes in the engine without a Duck, so it never waits."""
    d = Checked(graph([("I", 1, []), ("S", 1, ["I"]), ("H", 30, [])]), 1)
    d.refresh("S")
    d.run(3)  # the first build
    d.mark_unchanged("I.r0")
    d.refresh("H")  # H holds the only slot for 30 s
    d.run(0.5)
    d.state.slot_capacity[GROUP] = d.n = 2  # room for I, but not for anything else
    d.refresh("S")
    d.run(2)  # I runs, unchanged; S passes without a slot
    assert d.state.pond_states["S"].runs_completed == 2 and d.state.pending_passes


@pytest.mark.timeout(10)
def test_a_failed_pond_gives_its_slot_back():
    d = Checked(graph([("A", 2, []), ("B", 1, [])]), 1)
    d.fail_next("A.r0")
    d.refresh("A")
    d.refresh("B")  # waits behind A
    d.run(1)
    assert d.state.pond_states["B"].runs_started == 0
    d.run(1.5)  # A fails at 2 s and its slot goes to B
    assert d.state.pond_states["A"].is_failed and d.state.pond_states["B"].runs_started == 1


@pytest.mark.timeout(10)
def test_a_pipelined_run_of_a_busy_pond_takes_no_new_slot():
    d = Checked(chain(("P", [1, 4])), 1)  # P's first Ripple is free after 1 s, its second takes 4
    d.state.refreshes = {"P": [continuous("P")]}
    d.run(3)
    ps = d.state.pond_states["P"]
    assert ps.runs_started >= 2 and ps.slots == 1  # a second Run started while the first is in flight


@pytest.mark.timeout(30)
def test_the_throttle_paces_upstream_to_a_pond_waiting_for_a_slot():
    """An Inlet on another machine (no slots) feeding a Sink that waits behind a 30 s Run: demand reaching
    the waiting Sink marks it slow, so the Inlet isn't run over and over meanwhile."""
    def run(busy_while_waiting: bool) -> int:
        made = graph([("I", 1, []), ("S", 1, ["I"]), ("H", 30, [])])
        made[0].ponds["I"].pool = "remote"
        d = Checked(made, 1)
        d.refresh("H")
        d.state.refreshes = {"S": [continuous("S")]}
        if not busy_while_waiting:
            orig = engine._busy
            engine._busy = lambda s, rid: s.ripple_states[rid].is_running
        try:
            d.run(25)
        finally:
            if not busy_while_waiting:
                engine._busy = orig
        assert d.state.pond_states["S"].runs_started == 0  # still waiting behind H
        return d.state.pond_states["I"].runs_completed

    paced, unpaced = run(True), run(False)
    assert unpaced >= 15  # without it, the Inlet runs back to back for nothing
    assert paced <= 3


@pytest.mark.timeout(120)
@pytest.mark.parametrize("seed", range(12))
def test_random_pipelines_never_overcommit_and_always_drain(seed):
    """Random graphs, Ripple durations, Refreshes and slot counts (with exclusive and multi-slot needs): the
    slots in use never exceed the capacity, and once the Refreshes stop every Pond's demand is met and every
    slot is free."""
    rng = random.Random(seed)
    spec = []
    for i in range(rng.randint(3, 8)):
        name = f"P{i}"
        sources = [f"P{j}" for j in range(i) if rng.random() < 0.35]
        spec.append((name, [rng.choice([0.3, 1, 2, 4]) for _ in range(rng.randint(1, 3))], sources))
    made = graph(spec)
    n = rng.randint(1, 4)
    for name, _, _ in spec:
        made[0].ponds[name].slot_need = rng.choice([1, 1, 1, 2, n + 1])
    d = Checked(made, n)
    sinks = [name for name, _, _ in spec if not any(name in src for _, _, src in spec)]
    d.state.refreshes = {pid: [continuous(pid)] for pid in sinks if rng.random() < 0.6}
    for pid in sinks:
        d.refresh(pid)
    for step in range(600):  # 60 s of load, with an exclusive request now and then
        if step % 97 == 0:
            d.state.pond_states[rng.choice(spec)[0]].slot_exclusive = True
        d.step()
    d.state.refreshes = {}
    d.run(600, stop_when=lambda s: all(not ps.demand and not engine._in_flight(s, pid)
                                       for pid, ps in s.pond_states.items()))
    for pid, ps in d.state.pond_states.items():
        assert not [t for t in ps.demand if t > NEVER], (pid, ps.demand)
        assert ps.slots == 0 and ps.slot_wait_since is None
    assert d.now - T0 < secs(660)


@pytest.mark.timeout(30)
def test_a_pipelining_pond_yields_its_slot_to_a_waiting_one():
    """A Pond under a continuous Refresh whose Runs pipeline is never idle, so it would never give its slot
    back. While another Pond waits for one, a busy Duck starts no new Run: it drains, goes idle, and queues
    behind the waiting Pond."""
    d = Checked(graph([("P", [1, 4], []), ("Q", 1, [])]), 1)
    d.state.refreshes = {"P": [continuous("P")], "Q": [continuous("Q")]}
    d.run(60)
    assert d.state.pond_states["Q"].runs_completed >= 5
    assert d.state.pond_states["P"].runs_completed >= 5


@pytest.mark.timeout(10)
def test_a_reload_gives_runs_in_flight_their_slots_back():
    d = Checked(graph([("A", 5, []), ("B", 5, []), ("C", 1, [])]), 2)
    refresh_together(d, "A", "B")
    d.run(1)
    for ps in d.state.pond_states.values():
        ps.slots = 0  # a restart: the soft state is gone, the Runs are still in flight
    engine.restore_slots(d.state)
    assert used(d.state) == 2
    d.refresh("C")
    d.run(1)
    assert d.state.pond_states["C"].runs_started == 0  # nothing admitted over the resumed Runs
    d.run(5)  # A and B finish at 5 s; C runs after them
    assert d.state.pond_states["C"].runs_completed == 1


@pytest.mark.timeout(10)
def test_capacity_lowered_below_use_admits_nothing_until_it_drains():
    d = Checked(graph([("A", 3, []), ("B", 3, []), ("C", 1, [])]), 2)
    refresh_together(d, "A", "B")
    d.run(0.5)
    d.state.slot_capacity[GROUP] = 1  # lowered under two busy Ducks (the checker allows the overhang)
    d.n = 2
    d.refresh("C")
    d.run(2)
    assert d.state.pond_states["C"].runs_started == 0
    d.run(2)  # both drain; C gets the one slot
    assert d.state.pond_states["C"].runs_completed == 1 and used(d.state) <= 1


@pytest.mark.timeout(10)
def test_sleep_and_kill_take_a_pond_out_of_the_queue_and_a_kill_frees_its_slot():
    d = Checked(graph([("H", 3, []), ("W", 1, []), ("X", 1, [])]), 1)
    d.refresh("H")
    d.refresh("W")
    d.refresh("X")
    d.run(0.5)
    assert d.state.pond_states["W"].slot_wait_since is not None
    d.stop("W")  # Sleep clears W's demand: it leaves the queue
    d.run(0.2)
    assert d.state.pond_states["W"].slot_wait_since is None
    d.state = engine.kill_pond(d.state, "H", d.now)  # the holder is killed: its slot goes to X
    d.run(0.2)
    assert d.state.pond_states["X"].runs_started == 1 and d.state.pond_states["W"].runs_started == 0


@pytest.mark.timeout(30)
def test_a_refresh_target_waiting_for_a_slot_is_paced_and_still_runs():
    """The Outlet a continuous Refresh targets waits behind a long Run: its own demand marks it slow, it holds
    what reaches it, and it runs as soon as the slot frees, on its Source's newest output."""
    made = graph([("I", 1, []), ("O", 1, ["I"]), ("H", 10, [])])
    made[0].ponds["I"].pool = "remote"
    d = Checked(made, 1)
    d.refresh("H")
    d.state.refreshes = {"O": [continuous("O")]}
    d.run(9)
    assert d.state.pond_states["O"].runs_started == 0 and d.state.pond_states["I"].runs_completed <= 3
    d.run(3)
    o = d.starts["O"][0]
    assert o[1] == o[2]["I"]
    d.run(20)
    assert d.state.pond_states["O"].runs_completed >= 8


@pytest.mark.timeout(300)
@pytest.mark.parametrize("seed", range(40))
def test_random_pipelines_with_control_actions_never_overcommit_and_drain(seed):
    """Random graphs and slot counts under continuous and floating Refreshes, with Sleep, Kill, Force, Wake,
    failures and exclusive requests thrown in: the slots in use never exceed the capacity, and once the
    Refreshes stop (and the killed are cleared) every Pond's demand is met and every slot is free."""
    from test_engine import floating

    rng = random.Random(1000 + seed)
    spec = []
    for i in range(rng.randint(3, 9)):
        name = f"P{i}"
        sources = [f"P{j}" for j in range(i) if rng.random() < 0.35]
        spec.append((name, [rng.choice([0.2, 0.5, 1, 2, 5]) for _ in range(rng.randint(1, 3))], sources))
    made = graph(spec)
    n = rng.randint(1, 4)
    names = [name for name, _, _ in spec]
    for name in names:
        made[0].ponds[name].slot_need = rng.choice([1, 1, 1, 2, n + 1])
        if rng.random() < 0.15:
            made[0].ponds[name].pool = "remote"
    d = Checked(made, n)
    d.drift = rng.random() < 0.5
    d.state.refreshes = {pid: [continuous(pid) if rng.random() < 0.5 else floating(pid, rng.choice([1, 3, 7]))]
                         for pid in names if rng.random() < 0.4}
    for _ in range(900):
        r = rng.random()
        pid = rng.choice(names)
        if r < 0.01:
            d.state.pond_states[pid].slot_exclusive = True
        elif r < 0.015:
            d.stop(pid)
        elif r < 0.018:
            d.state = engine.kill_pond(d.state, pid, d.now)
        elif r < 0.024:
            d.state = engine.force_pond(d.state, pid, d.now)
        elif r < 0.03:
            d.state = engine.wake_pond(d.state, pid, d.now)
        elif r < 0.035:
            d.fail_next(rng.choice([rid for rid in d.state.ripples if d.state.ripples[rid].pond_id == pid]))
        elif r < 0.05:
            d.refresh(pid)
        d.step()
    d.state.refreshes = {}
    for pid in names:
        if d.state.pond_states[pid].is_killed or d.state.pond_states[pid].is_failed:
            d.state = engine.clear_pond(d.state, pid, d.now)
    d.run(900, stop_when=lambda s: all(not [t for t in ps.demand if t > NEVER] and not engine._in_flight(s, pid)
                                       for pid, ps in s.pond_states.items()))
    for pid, ps in d.state.pond_states.items():
        if ps.is_blocked or ps.is_failed:
            continue  # a failure downstream of a Source that failed again: blocked, by design
        assert not [t for t in ps.demand if t > NEVER], (pid, ps.demand)
        assert ps.slots == 0 and ps.slot_wait_since is None, pid
