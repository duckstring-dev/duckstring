"""The Catchment engine: the full freshness state machine (Ponds **and** Ripples, demand and Refreshes).

This is the orchestration brain the Catchment runs. It is a faithful port of the playground engine
(``playground/src/lib/orchestration.ts``): it models Ripple-level state so the throttle can pace demand to
the bottleneck. The difference from a flat simulator: it does not execute or time runs. It is *told* a
Ripple completed (via :func:`complete_ripple`, fed by Duck events), and every time it starts a Pond Run it
records a :class:`BeginRun` command on ``state.pending_begin_runs`` for the Catchment to dispatch to that
Pond's Duck.

There is one kind of demand: a set of requested freshnesses per Pond (``demand``), sent upstream from where
it is asked for (a Refresh on an Outlet) to the Inlets. Execution lives in the Duck; the Catchment owns
demand, Refreshes and freshness here. State in → state out; cascades mutate a clone synchronously to a
fixpoint, exactly as the playground does.
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
    Refresh,
    Ripple,
    RippleId,
    RippleState,
    ThrottleState,
    Window,
    max_demand,
    min_demand,
)

__all__ = [
    "NEVER",
    "ZERO",
    "BeginRun",
    "Pond",
    "PondState",
    "Ripple",
    "RippleState",
    "Refresh",
    "ThrottleState",
    "Window",
    "EngineState",
    "pond_source_f",
    "ripple_source_f",
    "add_demand",
    "demand_sources",
    "is_throttled",
    "resend_held_demand",
    "ripple_add_demand",
    "complete_ripple",
    "fail_ripple",
    "fail_pond",
    "required_sinks",
    "derive_blocked",
    "refresh_now",
    "sleep_pond",
    "wake_pond",
    "force_pond",
    "rebuild_pond",
    "repair_pond",
    "kill_pond",
    "clear_pond",
    "sentinel",
    "tick",
    "next_wake",
    "refresh_next_due",
    "drain_begin_runs",
]


@dataclass
class EngineState:
    ponds: dict[PondId, Pond] = field(default_factory=dict)
    pond_states: dict[PondId, PondState] = field(default_factory=dict)
    ripples: dict[RippleId, Ripple] = field(default_factory=dict)
    ripple_states: dict[RippleId, RippleState] = field(default_factory=dict)
    # Recurring Refreshes per Pond: any number scheduled, at most one floating (the Driver enforces it).
    refreshes: dict[PondId, list[Refresh]] = field(default_factory=dict)
    # Pond Run commands accumulated by start_pond_run; the Catchment drains these to dispatch to
    # Ducks. Ignored by in-process simulations/tests.
    pending_begin_runs: list[BeginRun] = field(default_factory=list)
    # Engine-synthesised passes (no-change Runs completed in-engine, no Duck): (pond_id, f) per pass.
    # The Catchment drains these to record a no-change pond_run row for history + reload counts.
    pending_passes: list[tuple[PondId, datetime]] = field(default_factory=list)
    # Memory slots per machine (plans/memory-budget.md): ``{pool: N}``. A Pond whose ``pool`` is here needs
    # a slot for its Duck to go from idle to busy; empty (the default, and the Playground) gates nothing.
    slot_capacity: dict[str, int] = field(default_factory=dict)

    def clone(self) -> EngineState:
        return EngineState(
            ponds=self.ponds,
            pond_states={k: v.copy() for k, v in self.pond_states.items()},
            ripples=self.ripples,
            ripple_states={k: v.copy() for k, v in self.ripple_states.items()},
            refreshes=self.refreshes,
            pending_begin_runs=list(self.pending_begin_runs),
            pending_passes=list(self.pending_passes),
            slot_capacity=dict(self.slot_capacity),
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


def add_demand(s: EngineState, pid: PondId, t: datetime, from_refresh: bool = False,
               release: bool = False) -> None:
    """A Pond is asked for data at least as fresh as ``t``: record it (if unsatisfied and new) and send it
    on to its demand Sources, unless the throttle holds it here (see "Throttle" below). ``from_refresh``
    marks a Refresh's own request (on this Pond, or a duct's from downstream), which makes this a trigger
    Pond. ``release`` marks a throttle release, which carries no epoch anyone asked for: an Inlet it reaches
    mints its freshness when the Run starts instead."""
    ps = s.pond_states[pid]
    if ps.is_blocked:  # no new demand enters a blocked Pond (and none propagates upstream from it)
        return
    if t <= ps.end_f or t in ps.demand:  # already satisfied, or already requested
        return
    if t <= ps.start_f:  # a Run already in flight at or past t will satisfy it (a repeat request for the
        return           # epoch it is running would otherwise re-run it, or complete it early as a pass)
    ps.demand.append(t)
    if release and not s.ponds[pid].sources:  # an Inlet: stamp the next Run's start (start_pond_run)
        _throttle(s, pid).released = True
    if from_refresh:
        _throttle(s, pid).trigger = True
    if not ps.repairing:  # the Driver sequences a repair; the throttle neither counts nor holds there
        _count_demand(s, pid)
        if _holds(s, pid):
            _throttle(s, pid).held += 1
            return
    _forward(s, pid, t, release=release)


def _local_demand(s: EngineState, pid: PondId, t: datetime | None) -> None:
    """Demand ``t`` at this Pond only: recorded, but neither counted by the throttle nor sent upstream. A
    Wake, a Spout's standing Wake and a missing-asset re-attempt use it: the Pond runs once its Sources
    reach ``t``, without soliciting them."""
    ps = s.pond_states[pid]
    if t is not None and t > ps.start_f and t > ps.end_f and t not in ps.demand:
        ps.demand.append(t)


def demand_sources(s: EngineState, pid: PondId) -> list[PondId]:
    """The Sources demand propagates to: the required ones (an optional Source is never waited for, so
    asking it for a freshness is pointless), plus, once, an **unbuilt** optional Source (never started a
    Run and holding no demand; a Sink would otherwise read output nobody builds). With no required
    Sources, all of them: the Pond runs on whichever is fresher, so it needs them asked. So an optional
    Source refreshes only through another path or its own Refresh."""
    pond = s.ponds[pid]
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if not required:
        return list(pond.sources)

    def unbuilt(sp: PondId) -> bool:
        sps = s.pond_states[sp]
        return sps.start_f == NEVER and not any(t > NEVER for t in sps.demand)

    return [sp for sp in pond.sources if sp not in pond.optional_sources or unbuilt(sp)]


def _forward(s: EngineState, pid: PondId, t: datetime, release: bool = False) -> None:
    """Send demand ``t`` from this Pond to its demand Sources, noting which run each Ripple was in.

    Sibling alignment: when some required demand Sources are throttled and others aren't, ``t`` goes to the
    throttled ones now and is held back from the rest until each throttled one has started a new Run (its
    turn; ``_flush_deferred``), so the Sources read at about the same time and the
    join's freshness isn't dragged down by a fast Source that read early."""
    th = _throttle(s, pid)
    th.held, th.completed = 0, False
    th.sent_at = {rid: s.ripple_states[rid].runs_started for rid in ripples_of(s, pid)}
    sources = demand_sources(s, pid)
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
            del th.deferred[sp]  # this demand covers what was held back from it
        add_demand(s, sp, t, release=release)
    if not th.deferred:
        th.deferred_at = {}


def _lead_sources(s: EngineState, pid: PondId, sources: list[PondId]) -> list[PondId]:
    """The throttled required Sources among ``sources``: the ones the others are aligned to."""
    optional = s.ponds[pid].optional_sources
    return [sp for sp in sources if sp not in optional and is_throttled(s, sp)]


def _flush_deferred(s: EngineState, pid: PondId) -> bool:
    """Send each held-back demand on once every throttled Source has started a new Run since it was held,
    or once none is throttled any more. A held-back Source that has since become throttled itself gets it
    at once; one that is no longer a demand Source (an optional Source now built) is dropped. Returns
    whether anything changed."""
    th = s.pond_states[pid].throttle
    if th is None or not th.deferred:
        return False
    sources = demand_sources(s, pid)
    lead = _lead_sources(s, pid, sources)
    due = all(s.pond_states[sp].runs_started > th.deferred_at.get(sp, -1) for sp in lead)
    changed = False
    for sp, (t, release) in list(th.deferred.items()):
        if due or sp in lead or sp not in sources:
            del th.deferred[sp]
            if sp in sources:
                add_demand(s, sp, t, release=release)
            changed = True
    if not th.deferred:
        th.deferred_at = {}
    return changed


# ─── Throttle (plans/throttle.md) ───────────────────────────────────────
#
# Paces demand that arrives faster than the pipeline can serve it; sparse demand never triggers it.
#
# Detection is per Ripple: a Ripple that sees two demands reach its Pond during one of its runs can't keep
# up, so it is *slow*, until it goes idle (an idle stage isn't the bottleneck: a stage below the bottleneck
# idles every cycle waiting for it, and a stray flag from one long run clears the same way). A Pond with a
# slow Ripple, or a throttled required Source, is *throttled*; that flag is all that passes between Ponds
# (read from Sources, like ``is_blocked``, and never across an optional edge).
#
# Two kinds of Pond hold the demand that reaches them, keeping it as their own demand (so they still run
# on anything fresher that arrives) but not passing it upstream straight away:
#   - a throttled trigger Pond (one a Refresh targets, normally the Outlet), released on its completions;
#   - any other Pond with a slow Ripple (the bottleneck), released on that Ripple's starts.
# Each sends one request upstream (at ``now``) per turn while it has unmet demand. So demand travels upstream
# at the bottleneck's pace, timed to its starts, and enters at the rate the Outlet is served, which bounds
# every path into it. A trigger Pond never paces on its own slow Ripple: it takes raw Refresh demand, so
# under a fast Refresh its Ripple always looks slow.
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
    """Re-send each unblocked Pond's newest unmet demand to its demand Sources. Held demand is ordinary
    (persisted) demand, but the throttle state that would release it is soft, so after a reload nothing
    else would send it on. Upstream ignores what is already requested or covered (rule 6), so this costs
    at most one extra upstream run per path."""
    for pid, ps in s.pond_states.items():
        unmet = [t for t in ps.demand if t > NEVER]
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
        if not _busy(s, rid):
            continue
        run, n = th.counts.get(rid, (rs.runs_started, 0))
        n = n + 1 if run == rs.runs_started else 1
        th.counts[rid] = (rs.runs_started, n)
        if n >= 2:
            th.slow.add(rid)


def _busy(s: EngineState, rid: RippleId) -> bool:
    """Busy, for the throttle: running, or a root Ripple of a Pond waiting for a memory slot. A Pond held for
    memory is a bottleneck like a slow one: demand reaching it while it waits counts, and as a bottleneck it
    releases upstream once its roots start, which is when its slot comes (plans/memory-budget.md)."""
    if s.ripple_states[rid].is_running:
        return True
    pid = s.ripples[rid].pond_id
    return s.pond_states[pid].slot_wait_since is not None and rid in roots_of(s, pid)


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
    if pond.is_draw or pond.is_spout or not demand_sources(s, pid):
        return False
    th = _throttle(s, pid)
    return is_throttled(s, pid) if th.trigger else bool(th.slow)


def _release_due(s: EngineState, pid: PondId) -> bool:
    """Is a holding Pond's turn due? A bottleneck's: every slow Ripple has started a new run since the last
    send. A trigger Pond's (or one whose slow Ripple has since gone idle): it completed a Pond Run since the
    last send (or has never sent), or it is no longer throttled."""
    th = s.pond_states[pid].throttle
    assert th is not None
    if th.slow and not th.trigger:
        return all(s.ripple_states[rid].runs_started > th.sent_at.get(rid, -1) for rid in th.slow)
    return th.held > 0 and (th.completed or not is_throttled(s, pid))


def _settle_throttle(s: EngineState, now: datetime) -> bool:
    """After the starts: clear slow Ripples that went idle, then let each Pond holding demand send one
    request upstream if its turn has come. Returns whether anything changed."""
    changed = False
    for pid, ps in s.pond_states.items():
        th = ps.throttle
        if th is None:
            continue
        idle = {rid for rid in th.slow if not _busy(s, rid)}
        if idle:
            th.slow -= idle
            changed = True
        if _flush_deferred(s, pid):
            changed = True
        if not any(t > NEVER for t in ps.demand):
            th.trigger, th.held = False, 0  # demand drained
            th.deferred, th.deferred_at = {}, {}
            continue
        if demand_sources(s, pid) and _release_due(s, pid):
            _forward(s, pid, now, release=True)
            changed = True
    return changed


def ripple_add_demand(s: EngineState, rid: RippleId, t: datetime) -> None:
    rs = s.ripple_states[rid]
    if t <= rs.end_f or t in rs.demand:
        return
    rs.demand.append(t)


# ─── Fault tolerance: blocked propagation ─────────────────────────────────────


def required_sinks(s: EngineState, pid: PondId) -> list[PondId]:
    """Ponds that depend on ``pid`` as a *required* Source (an optional Source never blocks a Sink)."""
    return [q.id for q in s.ponds.values() if pid in q.sources and pid not in q.optional_sources]


def derive_blocked(s: EngineState, pid: PondId) -> None:
    """Recompute ``is_blocked`` from this Pond's own failure and its required Sources, and — only if it
    changed — propagate to the Sinks so they re-derive. This is the single signal that travels
    downstream; a Pond still reads its blocked state solely from itself and its Sources.

    A Pond that unblocks re-sends its newest unmet demand to its demand Sources: demand forwarded while a
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
        unmet = [t for t in ps.demand if t > NEVER]
        if not blocked and unmet:
            _forward(s, pid, max(unmet))
        for q in required_sinks(s, pid):
            derive_blocked(s, q)


# ─── Lifecycle ────────────────────────────────────────────────────────────────


def block_on_missing_asset(state: EngineState, pid: PondId, reason: str, now: datetime) -> EngineState:
    """A Ripple read a Source asset that isn't published (:class:`~duckstring.core.MissingSourceAsset`).
    Park the Pond **blocked-with-a-reason** — *not* failed: no retry-budget burn, no failure alert. Abandon
    the incomplete Run (roll the phantom ``start_f → end_f`` so liveness won't fail it), record the reason,
    and keep the Run's freshness as **local** demand (not sent upstream) so the Pond re-attempts when its
    Source next publishes something fresher (it does not solicit the Source — auto-solicit is deferred). Propagates ``blocked``
    downstream. Recovers via :func:`clear_missing_asset` on the next successful Run. See plans/reset.md."""
    s = state.clone()
    ps = s.pond_states[pid]
    # Remember how fresh the Sources were when the read missed (their max end_f). The re-attempt is gated
    # on a Source completing a NEWER Run than this (can_start_pond) — the recovery signal is a genuine
    # Source republish, not the aggregate min-freshness (which the missing Source's own advance need not
    # move). Without this the rewound run re-fires at the same freshness every Duck round-trip — a busy-loop
    # that read-misses forever, floods run history, and starves the Catchment. See plans/reset.md.
    ps.missing_asset_f = max((source_visible_f(s, sp, pid) for sp in s.ponds[pid].sources), default=NEVER)
    abandoned = ps.start_f
    ps.start_f = ps.end_f  # abandon the incomplete run (the read failed; nothing published)
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.is_running = False
        rs.started_at = None
        rs.start_f = rs.end_f
        rs.demand = [t for t in rs.demand if t > rs.end_f]
    ps.missing_asset = reason
    _local_demand(s, pid, abandoned)  # retry when the Source is fresher, without soliciting it
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
        mt = min_demand(ps.demand)
        if mt is not None and f >= mt:
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
    and the Pond to ``start_f`` so its freshness climbs as on a real Run, but hold ``changed_f`` —
    downstream then sees no content change and passes in turn."""
    ps = s.pond_states[pid]
    target_f = ps.start_f
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.start_f = target_f
        rs.end_f = target_f
        rs.is_running = False
        rs.started_at = None
        rs.demand = [t for t in rs.demand if t > target_f]
    _complete_pond_run(s, pid, target_f, now, changed=False)
    s.pending_passes.append((pid, target_f))


def _must_run(s: EngineState, pid: PondId) -> bool:
    """Whether a Run of this Pond runs even with its Sources unchanged (never an engine pass)."""
    pond, ps = s.ponds[pid], s.pond_states[pid]
    return (not pond.sources or pond.always_run or pond.is_draw or pond.is_spout
            or ps.force_pending or ps.rebuild_pending or ps.repairing)


# ─── Memory slots (plans/memory-budget.md) ────────────────────────────────────
#
# A machine's Ducks share its memory as ``N`` slots (``EngineState.slot_capacity[pool]``). A Duck takes its
# slots when it goes from idle to busy and gives them back when nothing of its Pond is in flight; a pipelined
# Run of a busy Pond takes none. A Pond ready to start without a slot isn't started: its demand keeps
# collecting, and the Run it gets when its turn comes stamps its Sources' freshness then. FIFO by when a Pond
# began waiting, no backfilling (a head that can't fit holds the queue), and slots still free with nobody
# waiting go to the Ponds admitted in that pass, leaving one free while other demand is outstanding.


def _in_flight(s: EngineState, pid: PondId) -> bool:
    """Has this Pond's Duck work in flight (so it holds its slots)? A failed, killed or parked Pond doesn't."""
    if any_ripple_busy(s, pid):
        return True
    ps = s.pond_states[pid]
    return ps.start_f > ps.end_f and not ps.is_failed and not ps.is_killed


def _needs_slot(s: EngineState, pid: PondId) -> bool:
    """Would starting this Pond now take slots? Only for a Pond on a slotted machine whose Duck is idle and
    whose Run would dispatch (a pass, a Draw and a Spout run no Duck)."""
    pond, ps = s.ponds[pid], s.pond_states[pid]
    if pond.pool not in s.slot_capacity or pond.is_draw or pond.is_spout or ps.slots > 0:
        return False
    return _must_run(s, pid) or _pond_sources_changed(s, pid, ps.start_f)


def _yields(s: EngineState, pid: PondId) -> bool:
    """Does a busy Duck hold off its next pipelined Run? Yes while another Pond in its group waits for slots:
    otherwise a Pond whose Runs overlap under a continuous Refresh would never be idle, never give its slots
    back, and starve the queue. It drains, goes idle and queues like anyone else."""
    pond, ps = s.ponds[pid], s.pond_states[pid]
    if ps.slots == 0 or pond.pool not in s.slot_capacity:
        return False
    return any(s.pond_states[q].slot_wait_since is not None for q in s.ponds
               if q != pid and s.ponds[q].pool == pond.pool)


def restore_slots(s: EngineState) -> None:
    """After a reload (slots are soft state): give each Pond with a Run in flight on a slotted machine its slots
    back, so the Runs a restart resumes are counted and nothing is admitted over them. If they hold more than
    the capacity (it was lowered across the restart), nothing new starts until enough of them drain."""
    for pid, ps in s.pond_states.items():
        cap = s.slot_capacity.get(s.ponds[pid].pool)
        if cap is not None and not ps.slots and _in_flight(s, pid):
            ps.slots = max(1, min(s.ponds[pid].slot_need, cap))


def _release_slots(s: EngineState) -> None:
    for pid, ps in s.pond_states.items():
        if ps.slots and not _in_flight(s, pid):
            ps.slots = 0


def _admit(s: EngineState, ready: list[PondId], now: datetime, admitted: list[PondId]) -> bool:
    """Start the Ponds waiting for slots that fit, in queue order, adding them to ``admitted``. Returns whether
    any started."""
    for pid in ready:
        if s.pond_states[pid].slot_wait_since is None:
            s.pond_states[pid].slot_wait_since = now
    waiting = set(ready)
    for pid, ps in s.pond_states.items():
        if ps.slot_wait_since is not None and pid not in waiting:
            ps.slot_wait_since = None  # no longer ready (slept, killed, its demand cleared)
    started = False
    for group, cap in s.slot_capacity.items():
        members = [pid for pid in s.ponds if s.ponds[pid].pool == group]
        free = cap - sum(s.pond_states[pid].slots for pid in members)
        queue = sorted((pid for pid in ready if s.ponds[pid].pool == group),
                       key=lambda pid: (s.pond_states[pid].slot_wait_since, pid))
        for pid in queue:
            ps = s.pond_states[pid]
            need = cap if ps.slot_exclusive else max(1, min(s.ponds[pid].slot_need, cap))
            if need > free:
                break  # no backfilling: the head holds the queue until its slots free
            start_pond_run(s, pid, now)
            ps.slots, ps.slot_exclusive, ps.slot_wait_since = need, False, None
            free -= need
            admitted.append(pid)
            started = True
    return started


def _share_spare(s: EngineState, admitted: list[PondId]) -> None:
    """At the end of a pass, once demand has finished flowing: slots still free in a group nobody is waiting
    in go to the Ponds admitted in this pass, in queue order, keeping one free while another Pond of the
    group has outstanding demand. (Shared any earlier, the first Ponds admitted would take slots that Ponds
    the same pass makes ready a moment later need.)"""
    for group, cap in s.slot_capacity.items():
        members = [pid for pid in s.ponds if s.ponds[pid].pool == group]
        takers = [pid for pid in admitted if s.ponds[pid].pool == group and s.pond_states[pid].slots < cap]
        if not takers or any(s.pond_states[pid].slot_wait_since is not None for pid in members):
            continue
        free = cap - sum(s.pond_states[pid].slots for pid in members)
        others = any(pid not in admitted and not _in_flight(s, pid)
                     and any(t > NEVER for t in s.pond_states[pid].demand) for pid in members)
        spare = free - (1 if others else 0)
        i = 0
        while spare > 0:
            s.pond_states[takers[i % len(takers)]].slots += 1
            spare -= 1
            i += 1


def start_pond_run(s: EngineState, pid: PondId, now: datetime) -> None:
    ps = s.pond_states[pid]
    pond = s.ponds[pid]
    prior_f = ps.start_f  # the freshness this Run builds on (captured before the reset below)
    f, window_d = pond_source_f(s, pid, now)
    assert f is not None

    # Minted freshness: an Inlet stamps the demand epoch — the newest freshness it was asked for — not its
    # run-now. So a Refresh at T yields freshness T for every Inlet it reaches, whenever each physically
    # runs. Force's NEVER demand is filtered out, so a Force keeps the pond_source_f `now`. Windowed Inlets
    # and non-Inlets (and Draws) are unaffected — their freshness is window-end / source-derived / remote.
    # A throttle release mints at the start: it is pacing, and carries no epoch anyone asked for.
    if not pond.sources and not pond.windows and not pond.is_draw:
        ms = [t for t in ps.demand if t > NEVER]
        if ps.throttle is not None and ps.throttle.released:
            ms.append(now)
            ps.throttle.released = False
        if ms:
            f = max(ms)

    ps.start_f = f
    ps.demand = [t for t in ps.demand if t > ps.start_f]

    if not s.ponds[pid].sources:
        ps.d = window_d
    else:
        ds = [s.pond_states[sp].d for sp in s.ponds[pid].sources if s.pond_states[sp].end_f == ps.start_f]
        if ds:
            ps.d = max(ds)

    # Every Ripple must reach this freshness — stamped on ALL Ripples (run to completion). This
    # also initiates the run (roots have source_f == start_f).
    for rid in ripples_of(s, pid):
        ripple_add_demand(s, rid, ps.start_f)

    ps.runs_started += 1
    ps.gen_start_times[ps.runs_started] = now

    # Pass vs dispatch (plans/no-change-skip.md). A Pond with Sources whose content is all unchanged
    # since ``prior_f`` is completed in-engine as a **pass** (no Duck) — unless it must run regardless:
    # an Inlet (determines change by content, the engine can't), a Force/Rebuild/repair (recompute by
    # intent), an ``always_run`` side-effecting Pond, or a Draw/Spout (run off-Duck by the poller/egress
    # worker). Otherwise dispatch a BeginRun for the Duck (force = recompute; rebuild = cold rebuild).
    sources_changed = _pond_sources_changed(s, pid, prior_f)
    if _must_run(s, pid) or sources_changed:
        s.pending_begin_runs.append(BeginRun(
            pid, ps.start_f, force=ps.force_pending, rebuild=ps.rebuild_pending,
            sources_changed=sources_changed,
        ))
    else:
        _pass_pond_run(s, pid, now)
    ps.force_pending = False
    ps.rebuild_pending = False


def can_start_ripple(s: EngineState, rid: RippleId) -> bool:
    rs = s.ripple_states[rid]
    if rs.is_running:
        return False
    source_f = ripple_source_f(s, rid)
    mt = min_demand(rs.demand)
    return mt is not None and source_f >= mt


def start_ripple(s: EngineState, rid: RippleId, now: datetime) -> None:
    rs = s.ripple_states[rid]
    source_f = ripple_source_f(s, rid)
    rs.start_f = source_f
    rs.is_running = True
    rs.started_at = now
    rs.runs_started += 1
    rs.demand = [t for t in rs.demand if t > source_f]


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
    # A completed Run satisfies all demand up to its freshness — drop it so demand that was added
    # *during* the Run (valid then, ``t > end_f``) can't linger past completion and trigger a spurious
    # re-run at the same F. (start_pond_run clears only what existed at start.)
    ps.demand = [t for t in ps.demand if t > ps.end_f]
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


def complete_ripple(state: EngineState, rid: RippleId, now: datetime, changed: bool = True) -> EngineState:
    """Event: a Ripple's run finished (in the runtime, reported by the Duck). Adopts its parents'
    freshness and advances the Pond if it was the last leaf. ``changed``
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


def refresh_now(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Refresh a Pond now: ask for its data to be at least as fresh as ``now`` (a duct passes the
    downstream's epoch instead, so the upstream Inlet stamps the same freshness)."""
    s = state.clone()
    add_demand(s, pid, now, from_refresh=True)
    return s


def tap_due(state: EngineState, pid: PondId, now: datetime, fresh_within: timedelta) -> bool:
    """Whether a query through a Tap (plans/taps.md) sends the Pond a Refresh. It uses a floating Refresh's
    clock, evaluated per query: due once the newest request, or with none pending the freshness, is
    ``fresh_within`` old. A request already waiting for the Pond to start a Run covers the query, so a busy
    app adds at most one request beyond the Run in flight (0 still means every query between Runs)."""
    ps = state.pond_states.get(pid)
    if ps is None or ps.is_blocked:
        return False
    pending = max_demand(ps.demand)
    if pending is not None and pending > ps.start_f:
        return False
    return now + ps.d - _clock_ref(ps) >= fresh_within


def sleep_pond(state: EngineState, pid: PondId, now: datetime, upstream: bool = False) -> EngineState:
    """Sleep a Pond: clear its demand, but KEEP its Ripples' demand so any already-started Pond Run
    completes. With ``upstream=True`` the sleep propagates to every ancestor (a token following the
    source edges), clearing each one's demand too. The soft counterpart to Wake — it lets the Pond
    settle, vs Kill which cancels everything."""
    s = state.clone()
    seen: set[PondId] = set()
    queue = [pid]
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        ps = s.pond_states[cur]
        ps.demand = []  # (its Ripples keep theirs, so started runs complete)
        ps.throttle = None
        if upstream:
            for sp in s.ponds[cur].sources:
                if sp not in seen:
                    queue.append(sp)
    # Demand other Ponds still hold was cleared at the slept Sources they forwarded it to: send each one's
    # newest unmet demand to those Sources again, so another consumer's pending refresh isn't stranded.
    for q, qs in s.pond_states.items():
        unmet = [t for t in qs.demand if t > NEVER]
        if q in seen or qs.is_blocked or not unmet:
            continue
        held_back = qs.throttle.deferred if qs.throttle is not None else {}
        for sp in demand_sources(s, q):
            if sp in seen and sp not in held_back:
                add_demand(s, sp, max(unmet))
    return s


def wake_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Wake a Pond: one **local** request at its Sources' current freshness. The Pond runs once if its
    Sources already offer something fresher than its last Run; it does NOT solicit its Sources (that's a
    Refresh). An Inlet runs at ``now``. Also clears any failure/kill, so a parked Pond resumes."""
    s = state.clone()
    _clear_halt(s, pid)
    f, _ = pond_source_f(s, pid, now)
    _local_demand(s, pid, f)
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
    if NEVER not in ps.demand:
        ps.demand.append(NEVER)
    return s


def rebuild_pond(state: EngineState, pid: PondId, *, clear: bool = False) -> EngineState:
    """Flag a Pond so its **next** run is a Rebuild — a cold wipe-and-rebuild (the Duck drops the
    registry and reads its Sources in full). Lazy: it does *not* start a run, so the rebuild happens at
    the next genuinely-new freshness and propagates honestly (its rebuilt changelog floor rises above
    downstream consumers' watermarks). ``clear`` un-sets the flag. See ``plans/refresh.md``."""
    s = state.clone()
    s.pond_states[pid].rebuild_pending = not clear
    return s


def repair_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """A repair step (D3): rebuild **and** force this Pond at the current freshness — combine a cold
    rebuild with a one-shot run so the rebuild happens now, not on the next natural epoch. The Driver
    sequences these in topological order over a connected scope (see ``plans/refresh.md``)."""
    s = force_pond(state, pid, now)
    s.pond_states[pid].rebuild_pending = True
    return s


def kill_pond(state: EngineState, pid: PondId, now: datetime) -> EngineState:
    """Kill a Pond: cancel its in-flight Run (attributed to ``startF``) and park it in a terminal
    *killed* state. Clears all demand, stops its Ripples, blocks downstream, and supersedes retries —
    it stays down until an operator Wake/Force/Clear. The Catchment also terminates the Duck process."""
    s = state.clone()
    ps = s.pond_states[pid]
    ps.is_killed = True
    ps.demand = []
    ps.throttle = None
    for rid in ripples_of(s, pid):
        rs = s.ripple_states[rid]
        rs.is_running = False
        rs.started_at = None
        rs.demand = []
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
    admitted: list[PondId] = []  # Ponds given memory slots in this pass (_share_spare tops them up)
    # Start to a fixpoint, then settle the throttle: a release can enable more starts, so repeat
    # (bounded). Sends are decided only after the starts, so a slow Ripple that finished and restarted in
    # the same instant reads as one turnover, not as idle (clear, release) and then turned over (release).
    for _ in range(16):
        changed = True
        while changed:
            changed = False
            _release_slots(s)
            ready = []  # ready to start, but needing a memory slot (_admit decides)
            for pid in list(s.pond_states):
                if can_start_pond(s, pid, now):
                    if _needs_slot(s, pid):
                        ready.append(pid)
                        continue
                    if _yields(s, pid):
                        continue
                    start_pond_run(s, pid, now)
                    changed = True
            if (ready or any(ps.slot_wait_since is not None for ps in s.pond_states.values())) \
                    and _admit(s, ready, now, admitted):
                changed = True
            for rid in list(s.ripple_states):
                if can_start_ripple(s, rid):
                    start_ripple(s, rid, now)
                    started.append(rid)
                    changed = True
        if not _settle_throttle(s, now):
            break
    if admitted:
        _share_spare(s, admitted)
    return s, started


def tick(now: datetime, state: EngineState) -> EngineState:
    """Clock-driven processes only: the recurring Refreshes and the Spouts' standing Wake. Window
    availability is read live in :func:`can_start_pond`. The caller runs :func:`sentinel` afterwards."""
    s = state.clone()
    # Standing Wake (Spouts): whenever idle and armed, demand the Source's freshness locally (never sent on).
    # It then runs whenever the Source is fresher, at most once per window (the freshness is the window end),
    # and never adds upstream demand. Failed/killed/blocked Spouts stay parked until cleared.
    for pid, ps in s.pond_states.items():
        if not ps.standing_wake or ps.is_failed or ps.is_killed or ps.is_blocked:
            continue
        if ps.start_f == ps.end_f and not ps.demand and not any_ripple_busy(s, pid):
            f, _ = pond_source_f(s, pid, now)
            _local_demand(s, pid, f)

    # Recurring Refreshes. A floating one sends a request at ``now`` once ``every`` has passed since its clock
    # reference (the later of the newest pending request and the freshness; every 0 is continuous). Requests
    # pipeline freely and the throttle paces them to the bottleneck. A scheduled one requests its latest
    # occurrence, which the demand rules make idempotent (already requested, satisfied or in flight), so it
    # needs no "last fired" state and catches up after downtime with one request.
    for pid, refs in s.refreshes.items():
        ps = s.pond_states.get(pid)
        if ps is None:
            continue
        for r in refs:
            if r.scheduled:
                c = r.latest(now)
                if c is not None:
                    add_demand(s, pid, c, from_refresh=True)
            elif now + ps.d - _clock_ref(ps) >= r.every:
                add_demand(s, pid, now, from_refresh=True)
    return s


def _clock_ref(ps: PondState) -> datetime:
    """A floating Refresh's reference: the later of the Pond's newest pending request and its freshness. A
    request is only dropped once a Run at or past it has started, so this is never earlier than the newest
    request; and a Run the Refresh didn't ask for (a manual refresh, a Force, a Sink's demand) counts too. A
    strict cadence regardless of freshness is what a scheduled Refresh is for."""
    return max(max_demand(ps.demand) or NEVER, ps.start_f)


def refresh_next_due(state: EngineState, r: Refresh, now: datetime) -> datetime | None:
    """When a recurring Refresh next sends a request: a scheduled one's next occurrence, a floating one's
    deadline (``every`` after its clock reference, at the earliest ``now``). None for a continuous one
    (every 0: it asks whenever the Pond can take more) and for a schedule that has ended."""
    if r.scheduled:
        return r.next_occurrence(now)
    if r.every <= ZERO:
        return None
    ps = state.pond_states.get(r.pond_id)
    if ps is None:
        return None
    return max(now, _clock_ref(ps) + r.every - ps.d)


def next_wake(now: datetime, state: EngineState) -> datetime | None:
    """The earliest future instant the engine needs a :func:`tick`: the next window boundary of a
    windowed Inlet or Spout, or a recurring Refresh's next request. ``None`` if nothing is time-driven."""
    candidates: list[datetime] = []
    for pond in state.ponds.values():
        # Windowed Inlets (no sources) and windowed Spouts both advance on window boundaries.
        if (pond.sources and not pond.is_spout) or not pond.windows:
            continue
        for w in pond.windows:
            b = w.next_boundary(now)
            if b is not None:
                candidates.append(b)
    for refs in state.refreshes.values():
        for r in refs:
            if not r.scheduled and r.every <= ZERO:
                candidates.append(now)  # continuous: asks on every tick
                continue
            due = refresh_next_due(state, r, now)
            if due is not None:
                candidates.append(due)
    future = [c for c in candidates if c >= now]
    return min(future) if future else None
