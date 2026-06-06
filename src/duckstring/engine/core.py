"""Shared primitives for the orchestration engines.

The freshness/push-token model is split across two deployable engines: the
:mod:`~duckstring.engine.catchment` brain (full ponds + ripples, pull + push) that the Catchment
runs, and the push-only :mod:`~duckstring.engine.worker` that each Duck runs. Both build on the
dataclasses and helpers here so the rules are single-sourced.

Freshness ``F`` is a timezone-aware UTC ``datetime``; the window delay ``D`` and Tide bounds are
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


@dataclass(frozen=True)
class Window:
    """A recurring batch-availability window on an Inlet: opens on every ``cron`` fire and stays
    open (data "fresh until" the end) for ``duration``."""

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
    """A standing trigger on a Pond. ``wave`` re-Taps on completion/idle; ``tide`` is a clock that
    emits a push target ``now`` when the last requested freshness ages past ``bound``."""

    pond_id: PondId
    kind: str  # "wave" | "tide"
    bound: timedelta | None = None  # Tide only: the staleness bound.


@dataclass(frozen=True)
class BeginRun:
    """A command from the Catchment to a Duck: start a Pond Run at freshness ``f`` (push every Ripple
    to ``f``). Emitted by the Catchment's ``start_pond_run``; identified/idempotent by ``(pond_id, f)``."""

    pond_id: PondId
    f: datetime


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


# ─── Push target sets ─────────────────────────────────────────────────────────


def min_target(targets: list[datetime]) -> datetime | None:
    return min(targets) if targets else None


def max_target(targets: list[datetime]) -> datetime | None:
    return max(targets) if targets else None
