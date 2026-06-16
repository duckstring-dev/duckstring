"""The Catchment engine: the full freshness state machine (Ponds **and** Ripples, pull + push).

This is the orchestration brain the Catchment runs. It is a faithful port of the validated
playground engine — it models ripple-level state so the ripple pull cascade still drives multiple
Pond Runs (e.g. a single Tap advancing an Inlet through its internal depth) and the bottleneck
cadence. The difference from a flat simulator: it does not execute or time runs. It is *told* a
Ripple completed (via :func:`complete_ripple`, fed by Duck events), and every time it starts a Pond
Run it records a :class:`BeginRun` command on ``state.pending_begin_runs`` for the Catchment to
dispatch to that Pond's Duck.

Execution lives in the Duck (push-only); the Catchment owns all pull/triggers/freshness here. State
in → state out; cascades mutate a clone synchronously to a fixpoint, exactly as the playground did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .core import (
    NEVER,
    ZERO,
    BeginRun,
    Pond,
    PondId,
    PondState,
    Ripple,
    RippleId,
    RippleState,
    Trigger,
    Window,
    max_target,
    min_target,
)

__all__ = [
    "NEVER",
    "ZERO",
    "BeginRun",
    "Pond",
    "PondState",
    "Ripple",
    "RippleState",
    "Trigger",
    "Window",
    "EngineState",
    "pond_source_f",
    "ripple_source_f",
    "pond_receive_pull",
    "pond_set_has_pull",
    "ripple_set_has_pull",
    "pond_add_target",
    "ripple_add_target",
    "complete_ripple",
    "fail_ripple",
    "fail_pond",
    "required_sinks",
    "derive_blocked",
    "tap_pond",
    "pulse_pond",
    "sleep_pond",
    "wake_pond",
    "force_pond",
    "kill_pond",
    "clear_pond",
    "sentinel",
    "tick",
    "next_wake",
    "drain_begin_runs",
]


@dataclass
class EngineState:
    ponds: dict[PondId, Pond] = field(default_factory=dict)
    pond_states: dict[PondId, PondState] = field(default_factory=dict)
    ripples: dict[RippleId, Ripple] = field(default_factory=dict)
    ripple_states: dict[RippleId, RippleState] = field(default_factory=dict)
    triggers: dict[PondId, Trigger] = field(default_factory=dict)
    # Pond Run commands accumulated by start_pond_run; the Catchment drains these to dispatch to
    # Ducks. Ignored by in-process simulations/tests.
    pending_begin_runs: list[BeginRun] = field(default_factory=list)

    def clone(self) -> EngineState:
        return EngineState(
            ponds=self.ponds,
            pond_states={k: v.copy() for k, v in self.pond_states.items()},
            ripples=self.ripples,
            ripple_states={k: v.copy() for k, v in self.ripple_states.items()},
            triggers=self.triggers,
            pending_begin_runs=list(self.pending_begin_runs),
        )


def drain_begin_runs(state: EngineState) -> list[BeginRun]:
    """Return and clear the accumulated Pond Run commands (the Catchment dispatches these to Ducks)."""
    out = state.pending_begin_runs
    state.pending_begin_runs = []
    return out


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


def pond_source_f(s: EngineState, pid: PondId, now: datetime) -> tuple[datetime | None, timedelta]:
    pond = s.ponds[pid]
    if pond.is_draw:
        # A Pond Draw's freshness is the upstream freshness the poller mirrored in. NEVER (not yet
        # polled, or upstream never run) → cannot transfer.
        rf = s.pond_states[pid].remote_f
        return (rf if rf != NEVER else None), ZERO
    if not pond.sources:
        if pond.windows:
            best: tuple[datetime, timedelta] | None = None
            for w in pond.windows:
                end = w.active_end(now)
                if end is not None and (best is None or end < best[0]):
                    best = (end, w.duration)
            if best is not None:
                return best
            return None, ZERO  # between windows: cannot run
        return now, ZERO
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if required:
        return min(s.pond_states[sp].end_f for sp in required), ZERO
    return max(s.pond_states[sp].end_f for sp in pond.sources), ZERO


def ripple_source_f(s: EngineState, rid: RippleId) -> datetime:
    intra = intra_parents(s, rid)
    if not intra:
        return s.pond_states[s.ripples[rid].pond_id].start_f
    opt = s.ripples[rid].optional_parents
    req = [p for p in intra if p not in opt]
    if req:
        return min(s.ripple_states[p].end_f for p in req)
    return max(s.ripple_states[p].end_f for p in intra)


# ─── Demand reactions (synchronous cascades; mutate the working state) ─────────


def pond_receive_pull(s: EngineState, pid: PondId, now: datetime, m: datetime) -> None:
    """``m`` is the minted demand epoch carried by this pull — the freshness an Inlet it reaches will
    stamp. A cold-start (idle) receive passes ``m`` through unchanged so the whole cascade shares one
    epoch; a sustaining re-arm (from a running Pond's start) mints ``m`` at that Pond's start time."""
    ps = s.pond_states[pid]
    if ps.is_blocked:  # a blocked Pond solicits nothing new; it only drains existing demand
        ps.has_received_pull = False
        return
    if ps.start_f == ps.end_f:  # cold start: wake the whole Pond
        pond_set_has_pull(s, pid, now, m)
        for rid in ripples_of(s, pid):
            ripple_set_has_pull(s, rid, now, m)
    else:  # running: only sustain the leaves
        for rid in leaves_of(s, pid):
            ripple_set_has_pull(s, rid, now, m)
    ps.has_received_pull = False


def pond_set_has_pull(s: EngineState, pid: PondId, now: datetime, m: datetime) -> None:
    ps = s.pond_states[pid]
    if m > ps.pull_m:
        ps.pull_m = m  # the strongest (latest) pull epoch drives an Inlet's stamp
    if ps.has_pull:
        return
    ps.has_pull = True
    for sp in s.ponds[pid].sources:
        if s.pond_states[sp].start_f <= ps.start_f:
            s.pond_states[sp].has_received_pull = True
            pond_receive_pull(s, sp, now, m)  # cold-start cascade: pass the epoch through unchanged


def ripple_set_has_pull(s: EngineState, rid: RippleId, now: datetime, m: datetime) -> None:
    rs = s.ripple_states[rid]
    if rs.has_pull:
        return
    rs.has_pull = True
    intra = intra_parents(s, rid)
    if not intra:
        pond_set_has_pull(s, s.ripples[rid].pond_id, now, m)
    else:
        for p in intra:
            if s.ripple_states[p].start_f <= rs.start_f:
                ripple_set_has_pull(s, p, now, m)


def pond_add_target(s: EngineState, pid: PondId, t: datetime) -> None:
    ps = s.pond_states[pid]
    if ps.is_blocked:  # no new push enters a blocked Pond (and none propagates upstream from it)
        return
    if t <= ps.end_f or t in ps.targets:
        return
    ps.targets.append(t)
    for sp in s.ponds[pid].sources:
        pond_add_target(s, sp, t)


def ripple_add_target(s: EngineState, rid: RippleId, t: datetime) -> None:
    rs = s.ripple_states[rid]
    if t <= rs.end_f or t in rs.targets:
        return
    rs.targets.append(t)


# ─── Fault tolerance: blocked propagation ─────────────────────────────────────


def required_sinks(s: EngineState, pid: PondId) -> list[PondId]:
    """Ponds that depend on ``pid`` as a *required* Source (an optional Source never blocks a Sink)."""
    return [q.id for q in s.ponds.values() if pid in q.sources and pid not in q.optional_sources]


def derive_blocked(s: EngineState, pid: PondId) -> None:
    """Recompute ``is_blocked`` from this Pond's own failure and its required Sources, and — only if it
    changed — propagate to the Sinks so they re-derive. This is the single signal that travels
    downstream; a Pond still reads its blocked state solely from itself and its Sources."""
    ps = s.pond_states[pid]
    pond = s.ponds[pid]
    blocked = ps.is_failed or ps.is_killed or ps.remote_down or pond.has_missing_source or any(
        s.pond_states[sp].is_failed or s.pond_states[sp].is_blocked or s.pond_states[sp].is_killed
        for sp in pond.sources
        if sp not in pond.optional_sources
    )
    if blocked != ps.is_blocked:
        ps.is_blocked = blocked
        for q in required_sinks(s, pid):
            derive_blocked(s, q)


# ─── Lifecycle ────────────────────────────────────────────────────────────────


def can_start_pond(s: EngineState, pid: PondId, now: datetime) -> bool:
    ps = s.pond_states[pid]
    if ps.is_killed:  # terminal until an operator Wake/Force/Clear
        return False
    if s.ponds[pid].has_missing_source:  # a declared Source is absent — never run (hard block)
        return False
    f, _ = pond_source_f(s, pid, now)
    if f is None:
        return False
    if not ps.is_failed:  # a blocked-but-not-failed Pond still drains available Source freshness
        mt = min_target(ps.targets)
        if mt is not None and f >= mt:
            return True
        if ps.has_pull and f > ps.start_f:
            return True
    # Retry on change: a failed Pond with budget left re-runs when its Sources offer something fresher
    # than its last attempt (bypasses is_blocked — this is how the failure recovers).
    if ps.failed_f != NEVER and ps.failures <= s.ponds[pid].retry_on_change and f > ps.start_f:
        return True
    return False


def start_pond_run(s: EngineState, pid: PondId, now: datetime) -> None:
    ps = s.pond_states[pid]
    pond = s.ponds[pid]
    f, window_d = pond_source_f(s, pid, now)
    assert f is not None
    started_as_pull = ps.has_pull

    # Minted freshness: an Inlet stamps the demand epoch — the max minted ``m`` of its outstanding
    # demand (push ``m`` is the target value; pull ``m`` is ps.pull_m) — not its run-now. So a Pulse/
    # Tap at T yields freshness T for every Inlet it reaches, whenever each physically runs. Force's
    # NEVER target is filtered out, so a Force keeps the pond_source_f `now`. Windowed Inlets and
    # non-Inlets (and Draws) are unaffected — their freshness is window-end / source-derived / remote.
    if not pond.sources and not pond.windows and not pond.is_draw:
        ms = [t for t in ps.targets if t > NEVER]
        if started_as_pull and ps.pull_m > NEVER:
            ms.append(ps.pull_m)
        if ms:
            f = max(ms)

    # A blocked Pond drains but never solicits its Sources; a Wake (pull_local) also doesn't propagate.
    if started_as_pull and not ps.is_blocked and not ps.pull_local:
        for sp in pond.sources:
            s.pond_states[sp].has_received_pull = True
            pond_receive_pull(s, sp, now, now)  # sustain: mint this Pond's start time as the epoch

    ps.start_f = f
    ps.has_pull = False
    ps.pull_local = False
    ps.pull_m = NEVER
    ps.targets = [t for t in ps.targets if t > ps.start_f]

    if not s.ponds[pid].sources:
        ps.d = window_d
    else:
        ds = [s.pond_states[sp].d for sp in s.ponds[pid].sources if s.pond_states[sp].end_f == ps.start_f]
        if ds:
            ps.d = max(ds)

    # Every Ripple must reach this freshness — stamped on ALL Ripples (push to completion). This
    # also initiates the run (roots have source_f == start_f).
    for rid in ripples_of(s, pid):
        ripple_add_target(s, rid, ps.start_f)

    ps.runs_started += 1
    ps.gen_start_times[ps.runs_started] = now
    # Record the command for the Catchment to dispatch to this Pond's Duck (force = a recompute).
    s.pending_begin_runs.append(BeginRun(pid, ps.start_f, force=ps.force_pending))
    ps.force_pending = False


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
        for p in intra_parents(s, rid):
            ripple_set_has_pull(s, p, now, now)  # intra re-arm (same Pond) — epoch is moot here
        rs.has_pull = False
    rs.targets = [t for t in rs.targets if t > source_f]


def complete_ripple(state: EngineState, rid: RippleId, now: datetime) -> EngineState:
    """Event: a Ripple's run finished (in the runtime, reported by the Duck). Adopts its parents'
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
        # A completed Run satisfies every target up to its freshness — drop them so a target that was
        # added *during* the Run (valid then, ``t > end_f``) can't linger past completion and trigger a
        # spurious re-run at the same F. (start_pond_run clears only what existed at start.)
        ps.targets = [t for t in ps.targets if t > ps.end_f]
        ps.runs_completed += 1
        ps.completion_times.append(now)
        ps.gen_start_times.pop(ps.runs_completed, None)
        if ps.is_failed and ps.end_f > ps.failed_f:  # a Run fresher than the failure has succeeded
            ps.is_failed = False
            ps.failed_f = NEVER
            ps.failures = 0
            derive_blocked(s, pid)
        trig = s.triggers.get(pid)
        if trig is not None and trig.kind == "wave":
            pond_receive_pull(s, pid, now, now)  # Wave re-tap: a new demand epoch at completion
    return s


def fail_ripple(state: EngineState, rid: RippleId, now: datetime) -> EngineState:
    """Event: a Ripple gave up (the Duck exhausted its Pond Run's immediate-retry budget, reported via
    a ``failed`` event). The Pond has failed at the Run the Ripple was reaching (``Ripple.start_f``):
    record it, count it against ``retry_on_change``, and block downstream. Recovery is via the retry-
    on-change start condition (or an operator clear)."""
    s = state.clone()
    rs = s.ripple_states[rid]
    f = rs.start_f
    rs.is_running = False
    rs.started_at = None

    pid = s.ripples[rid].pond_id
    ps = s.pond_states[pid]
    ps.failed_f = max(ps.failed_f, f)  # gate clearing against the freshest failure
    ps.failures += 1  # every failed Run counts, even simultaneous ones
    ps.is_failed = True
    derive_blocked(s, pid)
    return s


def fail_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Fail an entire Pond, attributing it to the most recently started Pond Run (``start_f``). This is
    the failure with no single culprit Ripple: a Duck-level error (e.g. a failed ledger write) or a
    dead/unreachable Duck. No-op if nothing is in flight (``start_f <= end_f``). Stops any modelled
    Ripple execution and blocks downstream, exactly like a Ripple-level failure."""
    s = state.clone()
    ps = s.pond_states[pid]
    if ps.start_f <= ps.end_f:
        return s  # the latest Run already completed — nothing in flight to fail
    ps.failed_f = max(ps.failed_f, ps.start_f)
    ps.failures += 1
    ps.is_failed = True
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.is_running = False
        rs.started_at = None
    derive_blocked(s, pid)
    return s


# ─── Public entry points (operate on a clone, run cascades) ───────────────────


def tap_pond(state: EngineState, pid: PondId, now: datetime, m: datetime | None = None) -> EngineState:
    """One pull. ``m`` is the demand epoch to mint (defaults to ``now``); a duct passes the
    downstream's epoch so the upstream Inlet stamps the same freshness."""
    s = state.clone()
    pond_receive_pull(s, pid, now, m if m is not None else now)
    return s


def pulse_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    s = state.clone()
    pond_add_target(s, pid, now)
    return s


def sleep_pond(state: EngineState, pid: PondId, now: datetime, upstream: bool = False) -> EngineState:
    """Sleep a Pond: clear its push+pull demand and its Ripples' **pull** demand, but KEEP Ripple push
    targets so any already-started Pond Run completes. With ``upstream=True`` the sleep propagates to
    every ancestor (a token following the source edges), clearing each one's demand too. The soft
    counterpart to Wake — it lets the Pond settle, vs Kill which cancels everything."""
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
        ps.pull_m = NEVER
        ps.has_received_pull = False
        ps.targets = []
        for rid in ripples_of(s, cur):
            s.ripple_states[rid].has_pull = False  # keep targets (push) so started runs complete
        if upstream:
            for sp in s.ponds[cur].sources:
                if sp not in seen:
                    queue.append(sp)
    return s


def wake_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Wake a Pond: a one-shot, **non-propagating** pull. The Pond runs once when its Sources already
    offer something fresher than its last Run (``sourceF > startF``); it does NOT solicit its Sources
    (no upstream propagation — that's a Tap). Also clears any failure/kill, so a parked Pond resumes."""
    s = state.clone()
    _clear_halt(s, pid)
    ps = s.pond_states[pid]
    ps.has_pull = True
    ps.pull_local = True
    ps.pull_m = now  # Wake mints its own epoch (it sets has_pull directly, bypassing the cascade)
    return s


def force_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Force a Pond Run now — a recompute even with no upstream change (e.g. after a code patch). Resets
    the Pond's and its Ripples' ``endF`` so the idempotency guards re-execute them, and injects a
    one-shot demand. It runs at the **current** freshness, so ``endF`` returns unchanged and it does
    **not** propagate downstream. Clears any failure/kill (the operator override)."""
    s = state.clone()
    _clear_halt(s, pid)
    ps = s.pond_states[pid]
    ps.end_f = NEVER
    for rid in ripples_of(s, pid):
        s.ripple_states[rid].end_f = NEVER
    ps.force_pending = True
    if NEVER not in ps.targets:
        ps.targets.append(NEVER)
    return s


def kill_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Kill a Pond: cancel its in-flight Run (attributed to ``startF``) and park it in a terminal
    *killed* state. Clears all demand, stops its Ripples, blocks downstream, and supersedes retries —
    it stays down until an operator Wake/Force/Clear. The Catchment also terminates the Duck process."""
    s = state.clone()
    ps = s.pond_states[pid]
    ps.is_killed = True
    ps.has_pull = False
    ps.pull_m = NEVER
    ps.has_received_pull = False
    ps.pull_local = False
    ps.targets = []
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.is_running = False
        rs.started_at = None
        rs.has_pull = False
        rs.targets = []
    derive_blocked(s, pid)  # killed Pond blocks its Sinks
    return s


def clear_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Operator acknowledgement: clear a Pond's failure/kill/block without forcing a run. Downstream
    Ponds blocked only by this Pond re-derive and unblock on their own."""
    s = state.clone()
    _clear_halt(s, pid)
    return s


def _clear_halt(s: EngineState, pid: PondId) -> None:
    """Clear every operator/fault halt on a Pond (failure, kill) and re-derive downstream blocks."""
    ps = s.pond_states[pid]
    halted = ps.is_failed or ps.is_killed
    ps.is_failed = False
    ps.failed_f = NEVER
    ps.failures = 0
    ps.is_killed = False
    # Abandon the halted Run's phantom (start_f > end_f): without this the Pond looks perpetually
    # in-flight, and once failed_f is cleared the liveness sweep would re-fail it against a Duck that
    # is no longer there. Returning start_f to end_f leaves it genuinely idle, ready to run again.
    if halted:
        ps.start_f = ps.end_f
    derive_blocked(s, pid)  # may stay blocked if a required Source is still failed/blocked/killed


def sentinel(now: datetime, state: EngineState) -> tuple[EngineState, list[RippleId]]:
    """React to events: cascade pending demand and start everything runnable to a fixpoint. Returns
    the started Ripples (used by in-process sims); Pond Run commands accumulate on
    ``state.pending_begin_runs`` for the Catchment to dispatch."""
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
    """Clock-driven processes only: Tide target emission and Wave-on-idle re-Tap. Window availability
    is read live in :func:`can_start_pond`. The caller runs :func:`sentinel` afterwards."""
    s = state.clone()
    for pid, trig in s.triggers.items():
        ps = s.pond_states[pid]
        if trig.kind == "wave":
            idle = ps.start_f == ps.end_f and not ps.has_pull and not ps.targets and not any_ripple_busy(s, pid)
            if idle:
                pond_receive_pull(s, pid, now, now)  # Wave-on-idle: a fresh demand epoch
        elif trig.kind == "tide":
            bound = trig.bound or ZERO
            ref = max_target(ps.targets) or ps.start_f
            if now + ps.d - ref >= bound:
                pond_add_target(s, pid, now)
    return s


def next_wake(now: datetime, state: EngineState) -> datetime | None:
    """The earliest future instant the engine needs a :func:`tick`: the next window boundary of a
    windowed Inlet, or the next Tide deadline. ``None`` if nothing is time-driven."""
    candidates: list[datetime] = []
    for pond in state.ponds.values():
        if pond.sources or not pond.windows:
            continue
        for w in pond.windows:
            b = w.next_boundary(now)
            if b is not None:
                candidates.append(b)
    for pid, trig in state.triggers.items():
        if trig.kind != "tide":
            continue
        ps = state.pond_states[pid]
        ref = max_target(ps.targets) or ps.start_f
        deadline = ref + (trig.bound or ZERO) - ps.d
        candidates.append(deadline if deadline > now else now)
    future = [c for c in candidates if c >= now]
    return min(future) if future else None
