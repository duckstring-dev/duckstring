"""Pure freshness/push-token orchestration engine.

A faithful, behaviour-for-behaviour port of the playground simulator in
``frontend/src/lib/orchestration.ts`` (specified by ``docs/guide/theory.md`` — the "Pond State
Variables" pseudocode is the exact state machine). This module is the engine *core* only: no
FastAPI, DB, HTTP, or CLI. State in → state out.

Differences from the TypeScript *simulator*, which conflates concerns a real runtime must separate:

* **Time is real.** Freshness ``F``, ``now`` and push targets are timezone-aware UTC ``datetime``s;
  the window delay ``D``, Tide bounds and staleness are ``timedelta``s. Staleness = ``now + D - F``.
  The "never run" freshness is the sentinel :data:`NEVER` (``datetime.min``), which orders below
  every real timestamp — exactly as ``0`` did in the TS.
* **Windows are cron-like** (:class:`Window` = a cron expression + a duration), not per-minute
  seconds. Resolved with ``croniter``.
* **No run durations / no run timer.** A Ripple run takes however long the real process takes; the
  engine never simulates or auto-completes a run. It is *told* a run finished via
  :func:`complete_ripple`.
* **Event-driven, not tick-driven.** :func:`sentinel` runs on every event (a trigger firing, a run
  completing): it cascades demand to a fixpoint, records the start of everything runnable, and
  returns the list of Ripples the caller must actually launch. :func:`tick` handles only the
  clock-driven processes (Windows, Tide, Wave-on-idle); :func:`next_wake` says when it next needs to
  run, so the runtime can schedule rather than poll.

The engine indicates *that* a run can start and does not start it; the caller launches the real
process and reports completion back.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from croniter import croniter

PondId = str
RippleId = str

# Freshness sentinel for "never run". Orders below every real UTC timestamp, like ``0`` in the TS.
NEVER = datetime.min.replace(tzinfo=timezone.utc)
ZERO = timedelta(0)


# ─── Topology (immutable inputs) ──────────────────────────────────────────────


@dataclass(frozen=True)
class Window:
    """A recurring batch-availability window on an Inlet: opens on every ``cron`` fire and stays
    open (data "fresh until" the end) for ``duration``. Typically day-scale, but any scale works."""

    cron: str
    duration: timedelta


@dataclass
class Pond:
    id: PondId
    name: str
    sources: list[PondId] = field(default_factory=list)
    optional_sources: set[PondId] = field(default_factory=set)
    windows: list[Window] = field(default_factory=list)


@dataclass
class Ripple:
    id: RippleId
    pond_id: PondId
    name: str
    parents: list[RippleId] = field(default_factory=list)
    optional_parents: set[RippleId] = field(default_factory=set)


@dataclass
class Trigger:
    """A standing trigger attached to a Pond. ``wave`` re-Taps on completion/idle; ``tide`` is a
    clock that emits a push target ``now`` whenever the last requested freshness ages past ``bound``."""

    pond_id: PondId
    kind: str  # "wave" | "tide"
    bound: timedelta | None = None  # Tide only: the staleness bound.


# ─── Run state (mutable) ──────────────────────────────────────────────────────


@dataclass
class PondState:
    start_f: datetime = NEVER  # freshness of the most recently started Pond Run
    end_f: datetime = NEVER  # freshness of the most recently completed Pond Run
    d: timedelta = ZERO  # window delay carried by the current freshness
    has_received_pull: bool = False  # inbox: a Sink/trigger asked for resupply
    has_pull: bool = False  # a Pond Run is wanted in pull
    targets: list[datetime] = field(default_factory=list)  # unsatisfied push target freshnesses
    runs_started: int = 0
    runs_completed: int = 0
    gen_start_times: dict[int, datetime] = field(default_factory=dict)
    completion_times: list[datetime] = field(default_factory=list)

    def copy(self) -> PondState:
        return replace(
            self,
            targets=list(self.targets),
            gen_start_times=dict(self.gen_start_times),
            completion_times=list(self.completion_times),
        )


@dataclass
class RippleState:
    start_f: datetime = NEVER
    end_f: datetime = NEVER
    has_pull: bool = False
    targets: list[datetime] = field(default_factory=list)
    is_running: bool = False  # an in-flight run the engine is awaiting completion of
    started_at: datetime | None = None  # telemetry: when the in-flight run started
    runs_started: int = 0
    runs_completed: int = 0
    completion_times: list[datetime] = field(default_factory=list)

    def copy(self) -> RippleState:
        return replace(self, targets=list(self.targets), completion_times=list(self.completion_times))


@dataclass
class EngineState:
    ponds: dict[PondId, Pond] = field(default_factory=dict)
    pond_states: dict[PondId, PondState] = field(default_factory=dict)
    ripples: dict[RippleId, Ripple] = field(default_factory=dict)
    ripple_states: dict[RippleId, RippleState] = field(default_factory=dict)
    triggers: dict[PondId, Trigger] = field(default_factory=dict)

    def clone(self) -> EngineState:
        """A working copy: mutable run-state is deep-copied so cascades can mutate in place; the
        immutable topology dicts are shared. Mirrors ``clone()`` in orchestration.ts."""
        return EngineState(
            ponds=self.ponds,
            pond_states={k: v.copy() for k, v in self.pond_states.items()},
            ripples=self.ripples,
            ripple_states={k: v.copy() for k, v in self.ripple_states.items()},
            triggers=self.triggers,
        )


# ─── Push target sets ─────────────────────────────────────────────────────────


def min_target(targets: list[datetime]) -> datetime | None:
    return min(targets) if targets else None


def max_target(targets: list[datetime]) -> datetime | None:
    return max(targets) if targets else None


# ─── Topology helpers ─────────────────────────────────────────────────────────


def ripples_of(s: EngineState, pid: PondId) -> list[RippleId]:
    return [r.id for r in s.ripples.values() if r.pond_id == pid]


def intra_parents(s: EngineState, rid: RippleId) -> list[RippleId]:
    r = s.ripples[rid]
    return [p for p in r.parents if p in s.ripples and s.ripples[p].pond_id == r.pond_id]


def leaves_of(s: EngineState, pid: PondId) -> list[RippleId]:
    in_pond = ripples_of(s, pid)
    parented: set[RippleId] = set()
    for rid in in_pond:
        parented.update(intra_parents(s, rid))
    return [rid for rid in in_pond if rid not in parented]


def any_ripple_busy(s: EngineState, pid: PondId) -> bool:
    return any(s.ripple_states[rid].is_running for rid in ripples_of(s, pid))


# ─── Freshness derivation ─────────────────────────────────────────────────────


def _window_instance(w: Window, now: datetime) -> tuple[datetime, datetime]:
    """The current/most-recent instance of ``w`` as ``(start, end)``: the latest cron fire at or
    before ``now`` and its end ``start + duration``."""
    start = croniter(w.cron, now).get_prev(datetime)
    return start, start + w.duration


def pond_source_f(s: EngineState, pid: PondId, now: datetime) -> tuple[datetime | None, timedelta]:
    """A Pond's source freshness and the window delay it carries. ``F`` is ``None`` for an Inlet
    that is between windows (cannot run). For a windowed Inlet, ``F`` = the (soonest-ending) active
    window's end ("fresh until") and ``D`` = that window's duration. Mirrors orchestration.ts."""
    pond = s.ponds[pid]
    if not pond.sources:
        if pond.windows:
            best: tuple[datetime, timedelta] | None = None
            for w in pond.windows:
                _, end = _window_instance(w, now)
                if now < end and (best is None or end < best[0]):  # active, soonest-ending wins
                    best = (end, w.duration)
            if best is not None:
                return best
            return None, ZERO  # between windows: cannot run
        return now, ZERO  # live source
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if required:
        return min(s.pond_states[sp].end_f for sp in required), ZERO  # blocks on the stalest
    return max(s.pond_states[sp].end_f for sp in pond.sources), ZERO  # any optional Source suffices


def ripple_source_f(s: EngineState, rid: RippleId) -> datetime:
    intra = intra_parents(s, rid)
    if not intra:
        return s.pond_states[s.ripples[rid].pond_id].start_f  # root
    opt = s.ripples[rid].optional_parents
    req = [p for p in intra if p not in opt]
    if req:
        return min(s.ripple_states[p].end_f for p in req)
    return max(s.ripple_states[p].end_f for p in intra)


# ─── Demand reactions (synchronous cascades; mutate the working state) ─────────


def pond_receive_pull(s: EngineState, pid: PondId, now: datetime) -> None:
    ps = s.pond_states[pid]
    if ps.start_f == ps.end_f:  # cold start: wake the whole Pond
        pond_set_has_pull(s, pid, now)
        for rid in ripples_of(s, pid):
            ripple_set_has_pull(s, rid, now)
    else:  # running: only sustain the leaves
        for rid in leaves_of(s, pid):
            ripple_set_has_pull(s, rid, now)
    ps.has_received_pull = False


def pond_set_has_pull(s: EngineState, pid: PondId, now: datetime) -> None:
    ps = s.pond_states[pid]
    if ps.has_pull:
        return
    ps.has_pull = True
    for sp in s.ponds[pid].sources:
        # Cold-start propagation upstream: wake any Source that has not started work ahead of us
        # (Source.start_f <= our start_f). A Source already running ahead is skipped — its in-flight
        # Run will satisfy this demand, so re-arming it would over-pull.
        if s.pond_states[sp].start_f <= ps.start_f:
            sp_state = s.pond_states[sp]
            sp_state.has_received_pull = True
            pond_receive_pull(s, sp, now)


def ripple_set_has_pull(s: EngineState, rid: RippleId, now: datetime) -> None:
    rs = s.ripple_states[rid]
    if rs.has_pull:
        return
    rs.has_pull = True
    intra = intra_parents(s, rid)
    if not intra:
        pond_set_has_pull(s, s.ripples[rid].pond_id, now)  # root → lets the Pond start a Run as pull
    else:
        for p in intra:
            # Cold-start propagation between Ripples: wake any parent not started ahead of us.
            if s.ripple_states[p].start_f <= rs.start_f:
                ripple_set_has_pull(s, p, now)


def pond_add_target(s: EngineState, pid: PondId, t: datetime) -> None:
    """Record a push target (if unsatisfied and new) and propagate it eagerly upstream to Sources.
    The set keeps every outstanding request, not just the latest."""
    ps = s.pond_states[pid]
    if t <= ps.end_f or t in ps.targets:  # already satisfied, or already requested
        return
    ps.targets.append(t)
    for sp in s.ponds[pid].sources:  # propagate eagerly upstream (matches the TS: all Sources)
        pond_add_target(s, sp, t)


def ripple_add_target(s: EngineState, rid: RippleId, t: datetime) -> None:
    """Record a push target on a Ripple (from the Pond's run-start stamp). The Pond stamps every
    Ripple, so this never propagates further between Ripples."""
    rs = s.ripple_states[rid]
    if t <= rs.end_f or t in rs.targets:
        return
    rs.targets.append(t)


# ─── Lifecycle (internal — driven by sentinel) ────────────────────────────────


def can_start_pond(s: EngineState, pid: PondId, now: datetime) -> bool:
    ps = s.pond_states[pid]
    f, _ = pond_source_f(s, pid, now)
    if f is None:
        return False
    mt = min_target(ps.targets)
    if mt is not None and f >= mt:  # push: inputs satisfy the oldest outstanding request
        return True
    return ps.has_pull and f > ps.start_f  # pull with fresher input


def start_pond_run(s: EngineState, pid: PondId, now: datetime) -> None:
    ps = s.pond_states[pid]
    f, window_d = pond_source_f(s, pid, now)
    assert f is not None
    started_as_pull = ps.has_pull

    # A Sink starting as pull replenishes all its Sources (Kanban draw → restock); no-op for an
    # Inlet. Done before clearing has_pull.
    if started_as_pull:
        for sp in s.ponds[pid].sources:
            s.pond_states[sp].has_received_pull = True
            pond_receive_pull(s, sp, now)

    ps.start_f = f
    ps.has_pull = False
    ps.targets = [t for t in ps.targets if t > ps.start_f]  # this Run satisfies every target it reached

    # Window delay: from the window for an Inlet, else the worst-case of the deciding Sources.
    if not s.ponds[pid].sources:
        ps.d = window_d
    else:
        ds = [s.pond_states[sp].d for sp in s.ponds[pid].sources if s.pond_states[sp].end_f == ps.start_f]
        if ds:
            ps.d = max(ds)

    # Every Ripple must reach this freshness — stamped on ALL Ripples on every Run (pull or push), so
    # the whole Pond runs to completion (push-style). Roots have source_f == start_f, so this also
    # initiates the run.
    for rid in ripples_of(s, pid):
        ripple_add_target(s, rid, ps.start_f)

    ps.runs_started += 1
    ps.gen_start_times[ps.runs_started] = now


def can_start_ripple(s: EngineState, rid: RippleId) -> bool:
    rs = s.ripple_states[rid]
    if rs.is_running:
        return False
    source_f = ripple_source_f(s, rid)
    mt = min_target(rs.targets)
    if mt is not None and source_f >= mt:
        return True
    return rs.has_pull and source_f > rs.start_f


def start_ripple(s: EngineState, rid: RippleId, now: datetime) -> None:
    rs = s.ripple_states[rid]
    source_f = ripple_source_f(s, rid)
    rs.start_f = source_f
    rs.is_running = True
    rs.started_at = now
    rs.runs_started += 1

    if rs.has_pull:
        for p in intra_parents(s, rid):  # pull propagation upstream
            ripple_set_has_pull(s, p, now)
        rs.has_pull = False
    rs.targets = [t for t in rs.targets if t > source_f]  # this Run satisfies every target it reached


def complete_ripple(state: EngineState, rid: RippleId, now: datetime) -> EngineState:
    """Event: the caller reports that a Ripple's real run has finished. Adopts its parents'
    freshness, advances the Pond if it was the last leaf, and re-Taps a Wave on completion."""
    s = state.clone()
    rs = s.ripple_states[rid]
    rs.end_f = rs.start_f
    rs.is_running = False
    rs.started_at = None
    rs.runs_completed += 1
    rs.completion_times.append(now)

    pid = s.ripples[rid].pond_id
    ps = s.pond_states[pid]
    new_end = min(s.ripple_states[leaf].end_f for leaf in leaves_of(s, pid))
    if new_end > ps.end_f:
        ps.end_f = new_end
        ps.runs_completed += 1
        ps.completion_times.append(now)
        ps.gen_start_times.pop(ps.runs_completed, None)
        # Wave re-Taps each time its Pond completes a Run (an event, so handled here, not in tick).
        trig = s.triggers.get(pid)
        if trig is not None and trig.kind == "wave":
            pond_receive_pull(s, pid, now)
    return s


# ─── Public entry points (operate on a clone, run cascades) ───────────────────


def tap_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    s = state.clone()
    pond_receive_pull(s, pid, now)
    return s


def pulse_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    s = state.clone()
    pond_add_target(s, pid, now)
    return s


def stop_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Greedy stop: clear all pull/push demand up the whole ancestry. In-flight runs drain; no new
    runs start. Stops every upstream path even where other consumers are still queued."""
    s = state.clone()
    seen: set[PondId] = set()
    queue = [pid]
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        ps = s.pond_states[cur]
        ps.has_pull = False
        ps.has_received_pull = False
        ps.targets = []
        for rid in ripples_of(s, cur):
            s.ripple_states[rid].has_pull = False
            s.ripple_states[rid].targets = []
        for sp in s.ponds[cur].sources:
            if sp not in seen:
                queue.append(sp)
    return s


def sentinel(now: datetime, state: EngineState) -> tuple[EngineState, list[RippleId]]:
    """React to events: cascade pending demand and start everything runnable to a fixpoint (a start
    can cascade demand that enables more). Records starts in the returned state and hands back the
    list of Ripples whose real process the caller must now launch."""
    s = state.clone()
    started: list[RippleId] = []
    changed = True
    while changed:
        changed = False
        for pid in list(s.pond_states):
            if can_start_pond(s, pid, now):
                start_pond_run(s, pid, now)
                changed = True
        for rid in list(s.ripple_states):
            if can_start_ripple(s, rid):
                start_ripple(s, rid, now)
                started.append(rid)
                changed = True
    return s, started


def tick(now: datetime, state: EngineState) -> EngineState:
    """Clock-driven processes only: Tide target emission and Wave-on-idle re-Tap. Window
    availability is read live in :func:`can_start_pond`, so nothing is needed for it here. The caller
    runs :func:`sentinel` afterwards to act on any demand this raises."""
    s = state.clone()
    for pid, trig in s.triggers.items():
        ps = s.pond_states[pid]
        if trig.kind == "wave":
            idle = ps.start_f == ps.end_f and not ps.has_pull and not ps.targets and not any_ripple_busy(s, pid)
            if idle:
                pond_receive_pull(s, pid, now)
        elif trig.kind == "tide":
            # A clock: add a fresh target `now` when the freshness it last *requested* has itself aged
            # past `bound`. The reference is the newest pending target, else `start_f` (set to source_f
            # before targets clear, so it preserves the satisfied target).
            bound = trig.bound or ZERO
            ref = max_target(ps.targets) or ps.start_f
            if now + ps.d - ref >= bound:
                pond_add_target(s, pid, now)
    return s


def next_wake(now: datetime, state: EngineState) -> datetime | None:
    """The earliest future instant the engine needs a :func:`tick`: the next window boundary (open or
    the close of an active window) of any windowed Inlet, or the next Tide deadline. ``None`` if
    nothing is time-driven. Lets the runtime schedule a tick precisely instead of polling."""
    candidates: list[datetime] = []
    for pond in state.ponds.values():
        if pond.sources or not pond.windows:
            continue
        for w in pond.windows:
            _, end = _window_instance(w, now)
            if now < end:
                candidates.append(end)  # close of the currently-active window
            candidates.append(croniter(w.cron, now).get_next(datetime))  # next window open
    for pid, trig in state.triggers.items():
        if trig.kind != "tide":
            continue
        ps = state.pond_states[pid]
        ref = max_target(ps.targets) or ps.start_f
        deadline = ref + (trig.bound or ZERO) - ps.d
        candidates.append(deadline if deadline > now else now)
    future = [c for c in candidates if c >= now]
    return min(future) if future else None
