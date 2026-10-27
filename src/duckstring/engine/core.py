"""Shared primitives for the orchestration engines.

The freshness/demand model is split across two deployable engines: the
:mod:`~duckstring.engine.catchment` brain (full Ponds and Ripples, demand and Refreshes) that the Catchment
runs, and the :mod:`~duckstring.engine.worker` that each Duck runs. Both build on the dataclasses and
helpers here so the rules are single-sourced.

Freshness ``F`` is a timezone-aware UTC ``datetime``; the window delay ``D`` and a Refresh's period are
``timedelta``. The "never run" freshness is :data:`NEVER` (``datetime.min``), which orders below every
real timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

PondId = str
RippleId = str

# Freshness sentinel for "never run". Orders below every real UTC timestamp.
NEVER = datetime.min.replace(tzinfo=timezone.utc)
ZERO = timedelta(0)


# ─── Topology (immutable inputs) ──────────────────────────────────────────────


_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_UNIT_SECONDS = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800}


def _kept(t: datetime, valid_days: frozenset[str] | None, until: datetime | None) -> bool:
    """Whether a grid occurrence ``t`` is kept by a recurrence's day filter and end."""
    if until is not None and t > until:
        return False
    return valid_days is None or _DAYS[t.weekday()] in valid_days


def _latest(anchor: datetime, delta: timedelta, now: datetime) -> datetime | None:
    """The grid point ``anchor + k·delta`` (k ≥ 0) at or before ``now``; None before the anchor."""
    if now < anchor or delta.total_seconds() <= 0:
        return None
    return anchor + ((now - anchor) // delta) * delta


def _next_kept(anchor: datetime, delta: timedelta, now: datetime, valid_days: frozenset[str] | None,
               until: datetime | None, cap: int = 10000) -> datetime | None:
    """The next kept grid point strictly after ``now`` (bounded scan for a day filter)."""
    if delta.total_seconds() <= 0:
        return None
    k = 0 if now < anchor else (now - anchor) // delta + 1
    for i in range(cap):
        t = anchor + (k + i) * delta
        if until is not None and t > until:
            return None
        if valid_days is None or _DAYS[t.weekday()] in valid_days:
            return t
    return None


@dataclass(frozen=True)
class Window:
    """A recurring batch-availability window on an Inlet (RFC-5545-flavoured): the first window opens
    at ``start_anchor`` and stays open ("fresh until" the end) for ``duration``; it then recurs every
    ``freq_interval`` × ``freq_unit``. ``valid_days`` (a set of MON..SUN, or None) restricts which
    weekdays an occurrence is kept; ``until`` ends the recurrence. Occurrences are the grid
    ``start_anchor + k·delta`` (k ≥ 0), filtered by ``valid_days``/``until``."""

    start_anchor: datetime
    duration: timedelta
    freq_unit: str  # SECOND | MINUTE | HOUR | DAY | WEEK
    freq_interval: int = 1
    valid_days: frozenset[str] | None = None
    until: datetime | None = None

    def _delta(self) -> timedelta:
        return timedelta(seconds=_UNIT_SECONDS[self.freq_unit] * self.freq_interval)

    def active_end(self, now: datetime) -> datetime | None:
        """If ``now`` falls inside an occurrence, return that window's end ("fresh until"); else None.
        O(1): the most-recent grid point at/before ``now`` (windows are assumed non-overlapping)."""
        c = _latest(self.start_anchor, self._delta(), now)
        if c is None or not _kept(c, self.valid_days, self.until):
            return None
        end = c + self.duration
        return end if now < end else None

    def next_open(self, now: datetime, cap: int = 10000) -> datetime | None:
        """The next occurrence start strictly after ``now`` (bounded scan for ``valid_days``)."""
        return _next_kept(self.start_anchor, self._delta(), now, self.valid_days, self.until, cap)

    def next_boundary(self, now: datetime) -> datetime | None:
        """The soonest future instant the window's active-state changes (its close if active, or the
        next open) — used to schedule a tick."""
        cands = []
        end = self.active_end(now)
        if end is not None and end > now:
            cands.append(end)
        nxt = self.next_open(now)
        if nxt is not None and nxt > now:
            cands.append(nxt)
        return min(cands) if cands else None

    def occurrences(self, hstart: datetime, hend: datetime, cap: int = 2000) -> list[tuple[datetime, datetime]]:
        """Expand to explicit (start, end) windows intersecting [hstart, hend). Bounded by ``cap``;
        used only for the overlap check on add, not the per-tick hot path."""
        delta = self._delta()
        if delta.total_seconds() <= 0:
            return []
        if hstart <= self.start_anchor:
            k = 0
        else:
            k = max(0, (hstart - self.start_anchor) // delta - 1)  # back up one to catch a straddling window
        out: list[tuple[datetime, datetime]] = []
        for i in range(cap):
            t = self.start_anchor + (k + i) * delta
            if t >= hend or (self.until is not None and t > self.until):
                break
            if _kept(t, self.valid_days, None):
                te = t + self.duration
                if te > hstart:
                    out.append((t, te))
        return out


@dataclass
class Pond:
    id: PondId
    name: str
    sources: list[PondId] = field(default_factory=list)
    optional_sources: set[PondId] = field(default_factory=set)
    windows: list[Window] = field(default_factory=list)
    # Fault tolerance budgets (operational config, editable against the live Pond; default 0 = no
    # retries). ``retry_immediately`` is consumed per Pond Run by the Duck (Ripple-level retries);
    # ``retry_on_change`` governs how many failed Pond Runs the Catchment re-attempts on a Source
    # update. See docs/guide/theory.md "Fault Tolerance".
    retry_immediately: int = 0
    retry_on_change: int = 0
    # A side-effecting Pond that must run every time (e.g. a monitoring ping): it is never engine-passed
    # when its Sources are unchanged, so the Duck always runs (the Ripple gates its data work via
    # ``pond.sources_changed()`` / ``pond.skip()``). ORed up from its Ripples' ``always_run`` flags.
    always_run: bool = False
    # A Pond Draw (cross-Catchment): fed by a duct, not executed by a Duck. It has no local sources;
    # its freshness is the upstream freshness mirrored by the poller (PondState.remote_f), and its
    # single ripple performs the data transfer. See plans/cross-catchment-ducts.md.
    is_draw: bool = False
    # A Spout (egress): a terminal node hanging off its Source with a standing Wake — it delivers the
    # Source's output to an external system, is run by the egress worker (not a Duck), and never
    # propagates (a Wake up, terminal down). The egress dual of a Draw. See plans/egress.md.
    is_spout: bool = False
    # At least one declared Source (required OR optional) is absent from the Catchment — not deployed
    # and not drawn over a duct. The Pond cannot run with a missing dependency, so it is hard-blocked
    # until every Source is present (e.g. the Source is deployed, or a duct draws it in).
    has_missing_source: bool = False
    # The Pool (one shared filesystem — plans/persist.md) this Pond's Duck runs on: "catchment" for the
    # Catchment's own box, a Pool-of-one identity for a remote Duck. Same-Pool Sinks see this Pond's
    # output at ``end_f`` (the local publish is the handoff); cross-Pool Sinks at ``persisted_f``.
    pool: str = "catchment"
    # Whether this Pond publishes locally and mirrors to the durable plane ASYNCHRONOUSLY (a
    # Catchment-Pool Duck with a data root attached). False = the publish is already durable at
    # completion (no cloud, or a remote Duck writing the data root directly), so ``persisted_f``
    # tracks ``end_f`` and cross-Pool visibility never lags.
    async_persist: bool = False
    # Memory slots (plans/memory-budget.md): how many of its machine's slots this Pond's Duck needs when it
    # goes from idle to busy (more when a Ripple declares memory outside DuckDB). Used only when the
    # Pond's ``pool`` has a slot capacity (``EngineState.slot_capacity``).
    slot_need: int = 1


@dataclass
class Ripple:
    id: RippleId
    pond_id: PondId
    name: str
    parents: list[RippleId] = field(default_factory=list)
    optional_parents: set[RippleId] = field(default_factory=set)


@dataclass(frozen=True)
class Refresh:
    """A recurring Refresh on a Pond: a request for data at least as fresh as the moment it is sent, repeated.

    - **Floating** (no ``start``): due ``every`` after the later of the Pond's newest pending request and its
      freshness, so any refresh (a manual one, a scheduled one, a Sink's demand) resets its phase. ``every``
      of zero is continuous: as fresh as the pipeline allows, paced by the throttle.
    - **Scheduled** (a ``start``): requests each occurrence of the grid ``start + k·every`` (k ≥ 0), filtered
      by ``valid_days`` and ended by ``until`` as a window's grid is, whatever else refreshes the Pond.

    A Pond holds any number of scheduled Refreshes and at most one floating one (the Driver enforces it)."""

    pond_id: PondId
    name: str
    every: timedelta = ZERO
    start: datetime | None = None
    valid_days: frozenset[str] | None = None
    until: datetime | None = None

    @property
    def scheduled(self) -> bool:
        return self.start is not None

    def latest(self, now: datetime) -> datetime | None:
        """A scheduled Refresh's latest kept occurrence at or before ``now`` (None before its start, when that
        grid point is filtered out, or for a floating Refresh)."""
        if self.start is None:
            return None
        c = _latest(self.start, self.every, now)
        return c if c is not None and _kept(c, self.valid_days, self.until) else None

    def next_occurrence(self, now: datetime) -> datetime | None:
        """A scheduled Refresh's next kept occurrence strictly after ``now`` (None if it has ended)."""
        if self.start is None:
            return None
        return _next_kept(self.start, self.every, now, self.valid_days, self.until)


@dataclass(frozen=True)
class BeginRun:
    """A command from the Catchment to a Duck: start a Pond Run at freshness ``f`` (ask every Ripple
    for ``f``). Emitted by the Catchment's ``start_pond_run``; identified/idempotent by ``(pond_id, f)``.
    ``force`` re-runs every Ripple even if already fresh to ``f`` (a Force/recompute)."""

    pond_id: PondId
    f: datetime
    force: bool = False
    rebuild: bool = False  # a Rebuild: the Duck wipes the registry first → a cold full rebuild
    sources_changed: bool = True  # the engine's no-change verdict, exposed to Ripples as
                                  # pond.sources_changed() (an always_run Pond runs even when False)


# ─── Run state (mutable) ──────────────────────────────────────────────────────


@dataclass
class ThrottleState:
    """The throttle's per-Pond bookkeeping (plans/throttle.md). Soft: in memory only, rebuilt by
    observation; losing it falls back to unpaced demand until two demands land in one run again. Runs are
    identified by a Ripple's ``runs_started`` (a Ripple's runs never overlap), never by durations."""

    counts: dict[RippleId, tuple[int, int]] = field(default_factory=dict)  # ripple -> (run, demands seen in it)
    slow: set[RippleId] = field(default_factory=set)  # Ripples that saw two demands in one run, until idle
    held: int = 0  # demands held at this Pond since it last sent a request upstream
    sent_at: dict[RippleId, int] = field(default_factory=dict)  # each Ripple's runs_started at the last send
    trigger: bool = False  # a Refresh targets this Pond (cleared when its demand drains)
    # Completed a Pond Run since the last send, or never sent: a trigger Pond with no request outstanding has
    # nothing to wait for (else one already throttled through a Source another Refresh paces would hold its
    # first request until a completion that request is needed for).
    completed: bool = True
    released: bool = False  # Inlets: a release reached it, so its next Run stamps its start
    # Sibling alignment: demand held back from the Sources that aren't throttled until the throttled ones
    # start a new Run (Source -> (newest demand held back, whether a release)), and their runs_started then.
    deferred: dict[PondId, tuple[datetime, bool]] = field(default_factory=dict)
    deferred_at: dict[PondId, int] = field(default_factory=dict)

    def copy(self) -> ThrottleState:
        return replace(self, counts=dict(self.counts), slow=set(self.slow), sent_at=dict(self.sent_at),
                       deferred=dict(self.deferred), deferred_at=dict(self.deferred_at))


@dataclass
class PondState:
    start_f: datetime = NEVER  # freshness of the most recently started Pond Run
    end_f: datetime = NEVER  # freshness of the most recently completed Pond Run
    changed_f: datetime = NEVER  # freshness at which this Pond's OUTPUT last actually changed
                                 # (<= end_f). A pass advances end_f but holds this. See
                                 # plans/no-change-skip.md.
    persisted_f: datetime = NEVER  # freshness through which the published output is mirrored to the
                                   # durable plane (<= end_f; plans/persist.md). Tracks end_f exactly
                                   # for a non-async_persist Pond; advanced by record_persist for an
                                   # async one. Cross-Pool Sinks gate on THIS, same-Pool on end_f. A
                                   # data watermark, not demand — fail/clear/force never touch it.
    d: timedelta = ZERO  # window delay carried by the current freshness
    remote_f: datetime = NEVER  # Pond Draws only: the upstream freshness mirrored by the poller
                                # (transient — repopulated on each poll, not persisted)
    remote_down: bool = False  # Pond Draws only: upstream is failed/killed/blocked/unreachable →
                               # the Draw is blocked (drains landed data, solicits nothing)
    demand: list[datetime] = field(default_factory=list)  # unsatisfied requested freshnesses
    # Fault tolerance (see docs/guide/theory.md "Fault Tolerance").
    is_failed: bool = False  # a Pond Run gave up and has not been superseded by a fresher success
    is_blocked: bool = False  # this Pond is failed, or a required Source is failed/blocked/killed
    missing_asset: str | None = None  # a Ripple read an unpublished Source asset ("source.table") — the
                                      # Pond is blocked *waiting* for it, not failed (no budget, no alert).
                                      # Transient (re-derived on the next read attempt), see plans/reset.md.
    missing_asset_f: datetime = NEVER  # max Source end_f when the missing read happened: while parked,
                                       # re-attempt ONLY once a Source completes a newer Run than this (else
                                       # the rewound run would re-fire at the same freshness forever, a
                                       # busy-loop). Recovers on a genuine Source republish. Transient.
    failed_f: datetime = NEVER  # freshness of the freshest Pond Run that has failed (NEVER if none)
    failures: int = 0  # failed Pond Runs since the last success (counted against retry_on_change)
    # Control (Wake/Force/Kill — see docs).
    is_killed: bool = False  # operator Kill: terminal, supersedes retries, until Wake/Force/Clear
    standing_wake: bool = False  # a Spout: whenever idle, demand its Source's freshness locally (never
                                 # forwarded), so it runs on every Source advance without soliciting it.
                                 # Sleep/Kill disarm it; Wake/Force re-arm it.
    force_pending: bool = False  # next Run is a Force (recompute) — re-run Ripples even if unchanged
    rebuild_pending: bool = False  # next Run is a Rebuild — the Duck wipes state first for a cold rebuild
    repairing: bool = False  # in an active repair plan: blocked from normal demand until its turn (D3)
    runs_started: int = 0
    runs_completed: int = 0
    gen_start_times: dict[int, datetime] = field(default_factory=dict)
    completion_times: list[datetime] = field(default_factory=list)
    throttle: ThrottleState | None = None  # the throttle's soft state, created on first use
    # Memory slots (plans/memory-budget.md). ``slots``: held by this Pond's Duck while it has a Run in flight
    # (0 when idle). ``slot_wait_since``: when it became ready to start and couldn't get them (its place in
    # the FIFO queue; None when not waiting). ``slot_exclusive``: its next Run asks for every slot (a retry
    # after running out of memory). Soft, like the throttle.
    slots: int = 0
    slot_wait_since: datetime | None = None
    slot_exclusive: bool = False

    def copy(self) -> PondState:
        return replace(
            self,
            demand=list(self.demand),
            gen_start_times=dict(self.gen_start_times),
            completion_times=list(self.completion_times),
            throttle=self.throttle.copy() if self.throttle is not None else None,
        )


@dataclass
class RippleState:
    start_f: datetime = NEVER
    end_f: datetime = NEVER
    demand: list[datetime] = field(default_factory=list)
    is_running: bool = False  # an in-flight run the engine is awaiting completion of
    started_at: datetime | None = None  # telemetry: when the in-flight run started
    runs_started: int = 0
    runs_completed: int = 0
    completion_times: list[datetime] = field(default_factory=list)

    def copy(self) -> RippleState:
        return replace(self, demand=list(self.demand), completion_times=list(self.completion_times))


# ─── Demand sets ──────────────────────────────────────────────────────────────


def min_demand(demand: list[datetime]) -> datetime | None:
    return min(demand) if demand else None


def max_demand(demand: list[datetime]) -> datetime | None:
    return max(demand) if demand else None
