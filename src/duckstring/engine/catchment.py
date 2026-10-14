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
    ThrottleState,
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
    "ThrottleState",
    "Trigger",
    "Window",
    "EngineState",
    "pond_source_f",
    "ripple_source_f",
    "pond_receive_pull",
    "pond_set_has_pull",
    "ripple_set_has_pull",
    "pond_add_target",
    "push_sources",
    "is_throttled",
    "resend_held_demand",
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
    "refresh_pond",
    "repair_pond",
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
    # Engine-synthesised passes (no-change Runs completed in-engine, no Duck): (pond_id, f) per pass.
    # The Catchment drains these to record a no-change pond_run row for history + reload counts.
    pending_passes: list[tuple[PondId, datetime]] = field(default_factory=list)

    def clone(self) -> EngineState:
        return EngineState(
            ponds=self.ponds,
            pond_states={k: v.copy() for k, v in self.pond_states.items()},
            ripples=self.ripples,
            ripple_states={k: v.copy() for k, v in self.ripple_states.items()},
            triggers=self.triggers,
            pending_begin_runs=list(self.pending_begin_runs),
            pending_passes=list(self.pending_passes),
        )


def drain_begin_runs(state: EngineState) -> list[BeginRun]:
    """Return and clear the accumulated Pond Run commands (the Catchment dispatches these to Ducks)."""
    out = state.pending_begin_runs
    state.pending_begin_runs = []
    return out


def drain_passes(state: EngineState) -> list[tuple[PondId, datetime]]:
    """Return and clear the accumulated engine-synthesised passes (the Catchment records each as a
    no-change pond_run)."""
    out = state.pending_passes
    state.pending_passes = []
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


def roots_of(s: EngineState, pid: PondId) -> list[RippleId]:
    """A Pond's first Ripples: those with no parent inside the Pond (they read its Sources, or an Inlet's data)."""
    return [rid for rid in ripples_of(s, pid) if not intra_parents(s, rid)]


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
    # A Spout with windows delivers on the window clock — the *same* window mechanics as a windowed Inlet:
    # its freshness is the active window's end, so the standing Wake fires once per window (and holds
    # between windows, or until its source has published something to deliver). The worker still ships the
    # source's actual data at the source's real freshness; this is only the throttle clock.
    if pond.is_spout and pond.windows:
        if not any(s.pond_states[sp].end_f > NEVER for sp in pond.sources):
            return None, ZERO  # nothing published to deliver yet
        best: tuple[datetime, timedelta] | None = None
        for w in pond.windows:
            end = w.active_end(now)
            if end is not None and (best is None or end < best[0]):
                best = (end, w.duration)
        return best if best is not None else (None, ZERO)  # None = between windows: hold delivery
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
        return min(source_visible_f(s, sp, pid) for sp in required), ZERO
    return max(source_visible_f(s, sp, pid) for sp in pond.sources), ZERO


def source_visible_f(s: EngineState, sp: PondId, sink: PondId) -> datetime:
    """The freshness of Source ``sp``'s output AS VISIBLE to ``sink`` (the Pool-aware gating rule,
    plans/persist.md): its ``end_f`` when they share a Pool (the local publish is the handoff) or when
    the Source's publish is already durable at completion (``async_persist`` off — no cloud, or a remote
    Duck writing the data root directly); its ``persisted_f`` otherwise — a cross-Pool Sink can only read
    what the async mirror has landed on the durable plane. ``persisted_f <= end_f`` always, so this only
    ever *delays* a Sink, never invents freshness."""
    src = s.ponds[sp]
    if not src.async_persist or src.pool == s.ponds[sink].pool:
        return s.pond_states[sp].end_f
    return s.pond_states[sp].persisted_f


def record_persist(s: EngineState, pid: PondId, f: datetime) -> None:
    """The durable mirror for ``pid`` completed through ``f`` — advance its ``persisted_f`` watermark
    (monotonic; a replayed/stale report never regresses it). Cross-Pool Sinks waiting on this Source
    become runnable; the caller drives the cascade (``sentinel``/``_process``)."""
    ps = s.pond_states[pid]
    if f > ps.persisted_f:
        ps.persisted_f = f


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


def pond_add_target(s: EngineState, pid: PondId, t: datetime, from_trigger: bool = False,
                    release: bool = False) -> None:
    """A Pond receives push target ``t``: record it (if unsatisfied and new) and propagate it to its push
    Sources, unless the Pulse throttle holds it here (see "Pulse throttle" below). ``from_trigger`` marks
    a trigger's own demand (Pulse, Tide, a duct's Pulse), which makes this a trigger Pond. ``release``
    marks a throttle release, which carries no epoch anyone asked for: an Inlet it reaches mints its
    freshness when the Run starts instead."""
    ps = s.pond_states[pid]
    if ps.is_blocked:  # no new push enters a blocked Pond (and none propagates upstream from it)
        return
    if t <= ps.end_f or t in ps.targets:  # already satisfied, or already requested
        return
    if t <= ps.start_f:  # a Run already in flight at or past t will satisfy it (a repeat request for the
        return           # epoch it is running would otherwise re-run it, or complete it early as a pass)
    ps.targets.append(t)
    if release and not s.ponds[pid].sources:  # an Inlet: stamp the next Run's start (start_pond_run)
        _throttle(s, pid).released = True
    if from_trigger:
        _throttle(s, pid).trigger = True
    if not ps.repairing:  # the Driver sequences a repair; the throttle neither counts nor holds there
        _count_demand(s, pid)
        if _holds(s, pid):
            _throttle(s, pid).held += 1
            return
    _forward(s, pid, t, release=release)


def push_sources(s: EngineState, pid: PondId) -> list[PondId]:
    """The Sources push demand propagates to: the required ones (an optional Source is never waited for,
    so forcing it to a target is pointless), plus, once, an **unbuilt** optional Source (never started a
    Run and holding no target; a Sink would otherwise read output nobody builds). With no required
    Sources, all of them: the Pond runs on whichever is fresher, so it needs them asked. Pull is
    unaffected and still reaches every Source."""
    pond = s.ponds[pid]
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if not required:
        return list(pond.sources)

    def unbuilt(sp: PondId) -> bool:
        sps = s.pond_states[sp]
        return sps.start_f == NEVER and not any(t > NEVER for t in sps.targets)

    return [sp for sp in pond.sources if sp not in pond.optional_sources or unbuilt(sp)]


def _forward(s: EngineState, pid: PondId, t: datetime, release: bool = False) -> None:
    """Send push target ``t`` from this Pond to its push Sources, noting which run each Ripple was in.

    Sibling alignment: when some required push Sources are throttled and others aren't, ``t`` goes to the
    throttled ones now and is held back from the rest until each throttled one has started a new Run (its
    turn, when a Wave would re-arm; ``_flush_deferred``), so the Sources read at about the same time and the
    join's freshness isn't dragged down by a fast Source that read early."""
    th = _throttle(s, pid)
    th.held, th.completed = 0, False
    th.sent_at = {rid: s.ripple_states[rid].runs_started for rid in ripples_of(s, pid)}
    sources = push_sources(s, pid)
    lead = _lead_sources(s, pid, sources)
    hold = bool(lead) and len(lead) < len(sources)
    if hold and not th.deferred:
        th.deferred_at = {sp: s.pond_states[sp].runs_started for sp in lead}
    for sp in sources:
        if hold and sp not in lead:
            held = th.deferred.get(sp)
            if held is None or t > held[0]:
                th.deferred[sp] = (t, release)
            continue
        held = th.deferred.get(sp)
        if held is not None and held[0] <= t:
            del th.deferred[sp]  # this target covers what was held back from it
        pond_add_target(s, sp, t, release=release)
    if not th.deferred:
        th.deferred_at = {}


def _lead_sources(s: EngineState, pid: PondId, sources: list[PondId]) -> list[PondId]:
    """The throttled required Sources among ``sources``: the ones the others are aligned to."""
    optional = s.ponds[pid].optional_sources
    return [sp for sp in sources if sp not in optional and is_throttled(s, sp)]


def _flush_deferred(s: EngineState, pid: PondId) -> bool:
    """Send each held-back target on once every throttled Source has started a new Run since it was held,
    or once none is throttled any more. A held-back Source that has since become throttled itself gets its
    target at once; one that is no longer a push Source (an optional Source now built) is dropped. Returns
    whether anything changed."""
    th = s.pond_states[pid].throttle
    if th is None or not th.deferred:
        return False
    sources = push_sources(s, pid)
    lead = _lead_sources(s, pid, sources)
    due = all(s.pond_states[sp].runs_started > th.deferred_at.get(sp, -1) for sp in lead)
    changed = False
    for sp, (t, release) in list(th.deferred.items()):
        if due or sp in lead or sp not in sources:
            del th.deferred[sp]
            if sp in sources:
                pond_add_target(s, sp, t, release=release)
            changed = True
    if not th.deferred:
        th.deferred_at = {}
    return changed


# ─── Pulse throttle (plans/pulse-throttle.md) ─────────────────────────────────
#
# Paces push demand that arrives faster than the pipeline can serve it; sparse demand never triggers it.
#
# Detection is per Ripple: a Ripple that sees two demands reach its Pond during one of its runs can't keep
# up, so it is *slow*, until it goes idle (an idle stage isn't the bottleneck: a stage below the bottleneck
# idles every cycle waiting for it, and a stray flag from one long run clears the same way). A Pond with a
# slow Ripple, or a throttled required Source, is *throttled*; that flag is all that passes between Ponds
# (read from Sources, like ``is_blocked``, and never across an optional edge).
#
# Two kinds of Pond hold the demand that reaches them, keeping it as their own targets (so they still run
# on anything fresher that arrives) but not passing it upstream straight away:
#   - a throttled trigger Pond (normally the Outlet), released on its completions;
#   - any other Pond with a slow Ripple (the bottleneck), released on that Ripple's starts.
# Each sends one Pulse upstream (at ``now``) per turn while it has unmet demand. So demand travels upstream
# at the bottleneck's pace, timed to its starts (when a Wave would re-arm), and enters at the rate the
# Outlet is served, which bounds every path into it. A trigger Pond never paces on its own slow Ripple: it
# takes raw trigger demand, so under a fast trigger its Ripple always looks slow.
#
# At a join whose required Sources are only partly throttled, a request goes to the throttled ones at once and
# is held back from the others until each throttled one starts a new Run, so the Sources read together and the
# join's freshness isn't dragged down by a fast Source that read early (sibling alignment, ``_forward``).
#
# A release is pacing and carries no epoch anyone asked for, so an Inlet it reaches stamps its next Run's
# start rather than the release time (which can be a whole run earlier when the Inlet is busy). The state
# is soft (``PondState.throttle``, never persisted) and the throttle is silent: it changes only what is
# forwarded and how a released Inlet stamps.


def resend_held_demand(s: EngineState) -> None:
    """Re-send each unblocked Pond's newest unmet push target to its push Sources. Held demand is ordinary
    (persisted) targets, but the throttle state that would release it is soft, so after a reload nothing
    else would send it on. Upstream ignores what is already requested or covered (rule 6), so this costs
    at most one extra upstream run per path."""
    for pid, ps in s.pond_states.items():
        unmet = [t for t in ps.targets if t > NEVER]
        if unmet and not ps.is_blocked:
            _forward(s, pid, max(unmet))


def _throttle(s: EngineState, pid: PondId) -> ThrottleState:
    ps = s.pond_states[pid]
    if ps.throttle is None:
        ps.throttle = ThrottleState()
    return ps.throttle


def _count_demand(s: EngineState, pid: PondId) -> None:
    """Demand reached this Pond: count it against each running Ripple's current run."""
    th = _throttle(s, pid)
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        if not rs.is_running:
            continue
        run, n = th.counts.get(rid, (rs.runs_started, 0))
        n = n + 1 if run == rs.runs_started else 1
        th.counts[rid] = (rs.runs_started, n)
        if n >= 2:
            th.slow.add(rid)


def is_throttled(s: EngineState, pid: PondId) -> bool:
    """A slow Ripple of its own, or a throttled required Source (with only optional Sources, all of them,
    since it runs on whichever is fresher). Never across an optional edge."""
    th = s.pond_states[pid].throttle
    if th is not None and th.slow:
        return True
    pond = s.ponds[pid]
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if required:
        return any(is_throttled(s, sp) for sp in required)
    return bool(pond.sources) and all(is_throttled(s, sp) for sp in pond.sources)


def _holds(s: EngineState, pid: PondId) -> bool:
    """Does this Pond hold the demand reaching it? A trigger Pond while throttled; any other while it has
    a slow Ripple. A Pond with nowhere to send demand (an Inlet, a Draw) never holds."""
    pond = s.ponds[pid]
    if pond.is_draw or pond.is_spout or not push_sources(s, pid):
        return False
    th = _throttle(s, pid)
    return is_throttled(s, pid) if th.trigger else bool(th.slow)


def _release_due(s: EngineState, pid: PondId) -> bool:
    """Is a holding Pond's turn due? A bottleneck's: every slow Ripple has started a new run since the last
    send. A trigger Pond's (or one whose slow Ripple has since gone idle): it completed a Pond Run since the
    last send, or it is no longer throttled."""
    th = s.pond_states[pid].throttle
    assert th is not None
    if th.slow and not th.trigger:
        return all(s.ripple_states[rid].runs_started > th.sent_at.get(rid, -1) for rid in th.slow)
    return th.held > 0 and (th.completed or not is_throttled(s, pid))


def _settle_throttle(s: EngineState, now: datetime) -> bool:
    """After the starts: clear slow Ripples that went idle, then let each Pond holding demand send one
    Pulse upstream if its turn has come. Returns whether anything changed."""
    changed = False
    for pid, ps in s.pond_states.items():
        th = ps.throttle
        if th is None:
            continue
        idle = {rid for rid in th.slow if not s.ripple_states[rid].is_running}
        if idle:
            th.slow -= idle
            changed = True
        if _flush_deferred(s, pid):
            changed = True
        if not any(t > NEVER for t in ps.targets):
            th.trigger, th.held = False, 0  # demand drained
            th.deferred, th.deferred_at = {}, {}
            continue
        if push_sources(s, pid) and _release_due(s, pid):
            _forward(s, pid, now, release=True)
            changed = True
    return changed


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
    downstream; a Pond still reads its blocked state solely from itself and its Sources.

    A Pond that unblocks re-sends its newest unmet target to its push Sources: demand forwarded while a
    Source was blocked (or dropped there, as a halted Run's is) was refused, and would otherwise strand."""
    ps = s.pond_states[pid]
    pond = s.ponds[pid]
    blocked = ps.is_failed or ps.is_killed or ps.remote_down or ps.missing_asset is not None \
        or pond.has_missing_source or any(
        s.pond_states[sp].is_failed or s.pond_states[sp].is_blocked or s.pond_states[sp].is_killed
        or s.pond_states[sp].repairing  # a Source mid-repair: don't run downstream on its stale output
        for sp in pond.sources
        if sp not in pond.optional_sources
    )
    if blocked != ps.is_blocked:
        ps.is_blocked = blocked
        unmet = [t for t in ps.targets if t > NEVER]
        if not blocked and unmet:
            _forward(s, pid, max(unmet))
        for q in required_sinks(s, pid):
            derive_blocked(s, q)


# ─── Lifecycle ────────────────────────────────────────────────────────────────


def block_on_missing_asset(state: EngineState, pid: PondId, reason: str, now: datetime) -> EngineState:
    """A Ripple read a Source asset that isn't published (:class:`~duckstring.core.MissingSourceAsset`).
    Park the Pond **blocked-with-a-reason** — *not* failed: no retry-budget burn, no failure alert. Abandon
    the incomplete Run (roll the phantom ``start_f → end_f`` so liveness won't fail it), record the reason,
    and re-arm a **non-propagating** local pull so the Pond re-attempts when its Source next publishes
    something fresher (it does not solicit the Source — auto-solicit is deferred). Propagates ``blocked``
    downstream. Recovers via :func:`clear_missing_asset` on the next successful Run. See plans/reset.md."""
    s = state.clone()
    ps = s.pond_states[pid]
    # Remember how fresh the Sources were when the read missed (their max end_f). The re-attempt is gated
    # on a Source completing a NEWER Run than this (can_start_pond) — the recovery signal is a genuine
    # Source republish, not the aggregate min-freshness (which the missing Source's own advance need not
    # move). Without this the rewound run re-fires at the same freshness every Duck round-trip — a busy-loop
    # that read-misses forever, floods run history, and starves the Catchment. See plans/reset.md.
    ps.missing_asset_f = max((source_visible_f(s, sp, pid) for sp in s.ponds[pid].sources), default=NEVER)
    ps.start_f = ps.end_f  # abandon the incomplete run (the read failed; nothing published)
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.is_running = False
        rs.started_at = None
        rs.start_f = rs.end_f
        rs.targets = [t for t in rs.targets if t > rs.end_f]
    ps.missing_asset = reason
    ps.has_pull = True       # retry when the Source is fresher …
    ps.pull_local = True     # … without soliciting it (deferred auto-solicit)
    derive_blocked(s, pid)
    return s


def clear_missing_asset(state: EngineState, pid: PondId) -> EngineState:
    """Clear a Pond's missing-asset wait (its Source republished and it read clean) and re-derive blocked."""
    s = state.clone()
    if s.pond_states[pid].missing_asset is not None:
        s.pond_states[pid].missing_asset = None
        s.pond_states[pid].missing_asset_f = NEVER
        derive_blocked(s, pid)
    return s


def can_start_pond(s: EngineState, pid: PondId, now: datetime) -> bool:
    ps = s.pond_states[pid]
    if ps.is_killed:  # terminal until an operator Wake/Force/Clear
        return False
    pond = s.ponds[pid]
    # An Inlet waits until its first Ripples (the ones that read) are free: it stamps its freshness when the
    # Pond Run starts but reads when they run, so a Run queued behind a busy first Ripple would be labelled
    # up to a whole run older than its data. Later Ripples may still be busy; Runs pipeline through them.
    if not pond.sources and not pond.is_draw and any(s.ripple_states[r].is_running for r in roots_of(s, pid)):
        return False
    if ps.repairing and not ps.force_pending:  # in a repair plan, awaiting its turn (the plan releases
        return False                            # it by setting force_pending via repair_pond)
    if s.ponds[pid].has_missing_source:  # a declared Source is absent — never run (hard block)
        return False
    f, _ = pond_source_f(s, pid, now)
    if f is None:
        return False
    # Parked on a missing Source asset: re-attempt ONLY once a Source completes a newer Run than when we
    # read-and-missed — otherwise the rewound run re-fires at the same freshness forever (a Duck-round-trip
    # busy-loop). Keyed on max(Source end_f), not the min-based `f`, so the missing Source's own republish
    # (which need not raise the min) still triggers recovery. See plans/reset.md.
    if ps.missing_asset is not None:
        # Visible freshness (Pool-aware): a cross-Pool Source counts as republished only once its mirror
        # lands — the re-attempt would otherwise read the durable plane and miss again.
        src_f = max((source_visible_f(s, sp, pid) for sp in s.ponds[pid].sources), default=NEVER)
        if src_f <= ps.missing_asset_f:
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


def _pond_sources_changed(s: EngineState, pid: PondId, prior_f: datetime) -> bool:
    """Did any Source change its output since this Pond last ran (at ``prior_f``)? The content-aware
    skip test (plans/no-change-skip.md): ``max(Source.changedF) > prior_f`` over **all** Sources read
    (required ∪ optional — optional never triggers a run, but a triggered run still folds in whatever
    optional changed). Compared with strict ``>`` against the Pond's *prior* run freshness — a Source
    that changed exactly at ``prior_f`` was already incorporated. Sound (never misses a change); the
    only imprecision is a redundant run from a fresher non-binding Source, which is accepted.

    A Source with ``changed_f == NEVER`` but a real ``end_f`` (an under-initialised / pre-backfill
    state — production always stamps ``changed_f`` on the first completion) falls back to ``end_f``, so
    we assume changed rather than spuriously pass."""
    cfs = []
    for sp in s.ponds[pid].sources:
        sps = s.pond_states.get(sp)
        if sps is None:
            continue
        cfs.append(sps.changed_f if sps.changed_f > NEVER else sps.end_f)
    return bool(cfs) and max(cfs) > prior_f


def _pass_pond_run(s: EngineState, pid: PondId, now: datetime) -> None:
    """Synthesise a **pass**: complete this Pond Run with no Duck (no BeginRun). Advance every Ripple
    and the Pond to ``start_f`` so the heartbeat (freshness, re-arm, Wave re-tap) climbs as on a real
    Run, but hold ``changed_f`` — downstream then sees no content change and passes in turn."""
    ps = s.pond_states[pid]
    target_f = ps.start_f
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.start_f = target_f
        rs.end_f = target_f
        rs.is_running = False
        rs.started_at = None
        rs.targets = [t for t in rs.targets if t > target_f]
    _complete_pond_run(s, pid, target_f, now, changed=False)
    s.pending_passes.append((pid, target_f))


def start_pond_run(s: EngineState, pid: PondId, now: datetime) -> None:
    ps = s.pond_states[pid]
    pond = s.ponds[pid]
    prior_f = ps.start_f  # the freshness this Run builds on (captured before the reset below)
    f, window_d = pond_source_f(s, pid, now)
    assert f is not None
    started_as_pull = ps.has_pull

    # Minted freshness: an Inlet stamps the demand epoch — the max minted ``m`` of its outstanding
    # demand (push ``m`` is the target value; pull ``m`` is ps.pull_m) — not its run-now. So a Pulse/
    # Tap at T yields freshness T for every Inlet it reaches, whenever each physically runs. Force's
    # NEVER target is filtered out, so a Force keeps the pond_source_f `now`. Windowed Inlets and
    # non-Inlets (and Draws) are unaffected — their freshness is window-end / source-derived / remote.
    # A Pulse throttle release mints at the start: it is pacing, and carries no epoch anyone asked for.
    if not pond.sources and not pond.windows and not pond.is_draw:
        ms = [t for t in ps.targets if t > NEVER]
        if started_as_pull and ps.pull_m > NEVER:
            ms.append(ps.pull_m)
        if ps.throttle is not None and ps.throttle.released:
            ms.append(now)
            ps.throttle.released = False
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

    # Pass vs dispatch (plans/no-change-skip.md). A Pond with Sources whose content is all unchanged
    # since ``prior_f`` is completed in-engine as a **pass** (no Duck) — unless it must run regardless:
    # an Inlet (determines change by content, the engine can't), a Force/Refresh/repair (recompute by
    # intent), an ``always_run`` side-effecting Pond, or a Draw/Spout (run off-Duck by the poller/egress
    # worker). Otherwise dispatch a BeginRun for the Duck (force = recompute; refresh = cold rebuild).
    must_run = (
        not pond.sources or pond.always_run or pond.is_draw or pond.is_spout
        or ps.force_pending or ps.refresh_pending or ps.repairing
    )
    sources_changed = _pond_sources_changed(s, pid, prior_f)
    if must_run or sources_changed:
        s.pending_begin_runs.append(BeginRun(
            pid, ps.start_f, force=ps.force_pending, refresh=ps.refresh_pending,
            sources_changed=sources_changed,
        ))
    else:
        _pass_pond_run(s, pid, now)
    ps.force_pending = False
    ps.refresh_pending = False


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


def _complete_pond_run(s: EngineState, pid: PondId, new_end: datetime, now: datetime, changed: bool) -> None:
    """Advance a Pond Run to completion at ``new_end`` (the caller has established ``new_end > end_f``).
    Shared by ``complete_ripple`` (last leaf done) and ``_pass_pond_run`` (an engine-synthesised pass).
    ``changed`` advances ``changed_f`` only when the Run actually produced new output — a pass holds it."""
    ps = s.pond_states[pid]
    ps.end_f = new_end
    if not s.ponds[pid].async_persist:
        # The publish is already durable at completion (no cloud / a remote Duck writing the plane
        # directly) — the persist watermark tracks end_f exactly. An async_persist Pond's advances via
        # record_persist when its mirror completes (plans/persist.md).
        ps.persisted_f = new_end
    if changed:
        ps.changed_f = new_end
    # A completed Run satisfies every target up to its freshness — drop them so a target that was added
    # *during* the Run (valid then, ``t > end_f``) can't linger past completion and trigger a spurious
    # re-run at the same F. (start_pond_run clears only what existed at start.)
    ps.targets = [t for t in ps.targets if t > ps.end_f]
    ps.runs_completed += 1
    ps.completion_times.append(now)
    ps.gen_start_times.pop(ps.runs_completed, None)
    if ps.throttle is not None:
        ps.throttle.completed = True  # a trigger Pond's turn to release held demand (passes included)
    if ps.is_failed and ps.end_f > ps.failed_f:  # a Run fresher than the failure has succeeded
        ps.is_failed = False
        ps.failed_f = NEVER
        ps.failures = 0
        derive_blocked(s, pid)
    trig = s.triggers.get(pid)
    if trig is not None and trig.kind == "wave":
        pond_receive_pull(s, pid, now, now)  # Wave re-tap: a new demand epoch at completion


def complete_ripple(state: EngineState, rid: RippleId, now: datetime, changed: bool = True) -> EngineState:
    """Event: a Ripple's run finished (in the runtime, reported by the Duck). Adopts its parents'
    freshness, advances the Pond if it was the last leaf, and re-Taps a Wave on completion. ``changed``
    (carried by the Duck on the Run-completing Ripple event) decides whether the Pond's ``changed_f``
    advances — ``False`` for a Trickle with an empty delta or an explicit ``pond.skip()``."""
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
        _complete_pond_run(s, pid, new_end, now, changed)
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
    pond_add_target(s, pid, now, from_trigger=True)
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
        ps.throttle = None
        for rid in ripples_of(s, cur):
            s.ripple_states[rid].has_pull = False  # keep targets (push) so started runs complete
        if upstream:
            for sp in s.ponds[cur].sources:
                if sp not in seen:
                    queue.append(sp)
    # Demand other Ponds still hold was cleared at the slept Sources they forwarded it to: send each one's
    # newest unmet target to those Sources again, so another consumer's pending refresh isn't stranded.
    for q, qs in s.pond_states.items():
        unmet = [t for t in qs.targets if t > NEVER]
        if q in seen or qs.is_blocked or not unmet:
            continue
        held_back = qs.throttle.deferred if qs.throttle is not None else {}
        for sp in push_sources(s, q):
            if sp in seen and sp not in held_back:
                pond_add_target(s, sp, max(unmet))
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


def refresh_pond(state: EngineState, pid: PondId, *, clear: bool = False) -> EngineState:
    """Flag a Pond so its **next** run is a Refresh — a cold wipe-and-rebuild (the Duck drops the
    registry and reads its Sources in full). Lazy: it does *not* start a run, so the refresh happens at
    the next genuinely-new freshness and propagates honestly (its rebuilt changelog floor rises above
    downstream consumers' watermarks). ``clear`` un-sets the flag. See ``plans/refresh.md``."""
    s = state.clone()
    s.pond_states[pid].refresh_pending = not clear
    return s


def repair_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """A repair step (D3): refresh **and** force this Pond at the current freshness — combine a cold
    rebuild with a one-shot run so the rebuild happens now, not on the next natural epoch. The Driver
    sequences these in topological order over a connected scope (see ``plans/refresh.md``)."""
    s = force_pond(state, pid, now)
    s.pond_states[pid].refresh_pending = True
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
    ps.throttle = None
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
    Ponds blocked only by this Pond re-derive and unblock on their own. Drops the Pond's throttle state."""
    s = state.clone()
    s.pond_states[pid].throttle = None
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
    # Start to a fixpoint, then settle the Pulse throttle: a release can enable more starts, so repeat
    # (bounded). Sends are decided only after the starts, so a slow Ripple that finished and restarted in
    # the same instant reads as one turnover, not as idle (clear, release) and then turned over (release).
    for _ in range(16):
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
        if not _settle_throttle(s, now):
            break
    return s, started


def restore_demand(s: EngineState, now: datetime) -> None:
    """Re-derive **pull** demand from the sinks — a corrective invariant that makes the one-shot pull
    propagation *eventually consistent*. A Pond still holding a propagating pull it can't yet satisfy
    implies its behind Sources (the cold-start test ``S.start_f <= D.start_f``) should hold pulls too; if a
    reset / a crash between propagate-and-persist / a blocked-Source refusal dropped that upstream, this
    re-solicits it. ``pond_receive_pull`` is idempotent (the ``has_pull`` guard) and **refuses blocked
    Ponds**, so a path automatically parks under a blocked Source and heals on the first tick after it
    unblocks. Excludes killed Ponds (parked terminal) and ``pull_local`` Wakes (non-propagating).

    *Push* is deliberately not restored here: a standing Tide re-emits its targets every bound (its own
    restoration), and the narrow "a one-shot Pulse hit a blocked Source" strand is healed by Mechanism 2's
    solicitation (a pull), not by blanket per-tick target re-propagation — which churns the Tide throttle
    (targets encode the cadence). See plans/reset.md."""
    for pid in list(s.pond_states):
        ps = s.pond_states[pid]
        if ps.is_killed or ps.pull_local or ps.is_blocked or not ps.has_pull:
            continue
        for sp in s.ponds[pid].sources:
            if s.pond_states[sp].start_f <= ps.start_f:
                s.pond_states[sp].has_received_pull = True
                pond_receive_pull(s, sp, now, ps.pull_m)


def tick(now: datetime, state: EngineState) -> EngineState:
    """Clock-driven processes only: Tide target emission and Wave-on-idle re-Tap, plus the demand-restoration
    invariant (:func:`restore_demand`). Window availability is read live in :func:`can_start_pond`. The
    caller runs :func:`sentinel` afterwards."""
    s = state.clone()
    # Standing Wake (Spouts): whenever idle and armed, re-arm a **non-propagating** pull (a Wake, not a
    # Wave — `pull_local` stops `start_pond_run` soliciting the Source). It then runs whenever the Source
    # is fresher (`can_start_pond`), at most once per window (the freshness is the window end), and never
    # adds upstream demand. Failed/killed Spouts stay parked until cleared.
    for pid in s.pond_states:
        ps = s.pond_states[pid]
        if not ps.standing_wake or ps.is_failed or ps.is_killed or ps.is_blocked:
            continue
        idle = ps.start_f == ps.end_f and not ps.has_pull and not ps.targets and not any_ripple_busy(s, pid)
        if idle:
            ps.has_pull = True
            ps.pull_local = True
            for rid in ripples_of(s, pid):
                s.ripple_states[rid].has_pull = True

    for pid, trig in s.triggers.items():
        ps = s.pond_states[pid]
        if trig.kind == "wave":
            idle = ps.start_f == ps.end_f and not ps.has_pull and not ps.targets and not any_ripple_busy(s, pid)
            if idle:
                pond_receive_pull(s, pid, now, now)  # Wave-on-idle: a fresh demand epoch
        elif trig.kind == "tide":
            bound = trig.bound or ZERO
            ref = _clock_ref(ps)
            if now + ps.d - ref >= bound:
                pond_add_target(s, pid, now, from_trigger=True)
    restore_demand(s, now)  # re-derive any demand lost upstream (reset / crash / blocked-refusal)
    return s


def _clock_ref(ps: PondState) -> datetime:
    """The floating refresh's reference: the later of its newest pending request and the Pond's freshness. A
    request is only dropped once a Run at or past it has started, so this is never earlier than the newest
    request; and a Run the Tide didn't ask for (a manual refresh, a Force, a Sink's demand) counts too. A strict
    cadence regardless of freshness is what an anchored refresh is for."""
    return max(max_target(ps.targets) or NEVER, ps.start_f)


def next_wake(now: datetime, state: EngineState) -> datetime | None:
    """The earliest future instant the engine needs a :func:`tick`: the next window boundary of a
    windowed Inlet, or the next Tide deadline. ``None`` if nothing is time-driven."""
    candidates: list[datetime] = []
    for pond in state.ponds.values():
        # Windowed Inlets (no sources) and windowed Spouts both advance on window boundaries.
        if (pond.sources and not pond.is_spout) or not pond.windows:
            continue
        for w in pond.windows:
            b = w.next_boundary(now)
            if b is not None:
                candidates.append(b)
    for pid, trig in state.triggers.items():
        if trig.kind != "tide":
            continue
        ps = state.pond_states[pid]
        ref = _clock_ref(ps)
        deadline = ref + (trig.bound or ZERO) - ps.d
        candidates.append(deadline if deadline > now else now)
    future = [c for c in candidates if c >= now]
    return min(future) if future else None
