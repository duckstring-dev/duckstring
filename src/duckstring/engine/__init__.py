"""Duckstring orchestration engine (the freshness and demand model).

Two deployable engines share a core:

- :mod:`.catchment` — the full state machine (Ponds and Ripples, demand and Refreshes) the Catchment runs.
  Its public functions are re-exported here, so ``duckstring.engine`` is the composed engine used by the
  simulation in ``tests/test_engine.py`` and by the Catchment runtime.
- :mod:`.worker` — the engine each Duck runs to execute a Pond Run to completion.
- :mod:`.pond` — the per-Pond run ledger (``ponds/{base_pond}/pond.db``).

Pure: no FastAPI/DB/HTTP imports here.
"""

from __future__ import annotations

from . import pond, worker
from .catchment import (
    EngineState,
    add_demand,
    block_on_missing_asset,
    clear_missing_asset,
    clear_pond,
    complete_ripple,
    demand_sources,
    derive_blocked,
    drain_begin_runs,
    drain_passes,
    fail_pond,
    fail_ripple,
    force_pond,
    is_throttled,
    kill_pond,
    next_wake,
    pond_source_f,
    rebuild_pond,
    record_persist,
    refresh_next_due,
    refresh_now,
    repair_pond,
    required_sinks,
    resend_held_demand,
    restore_slots,
    ripple_add_demand,
    ripple_source_f,
    sentinel,
    sleep_pond,
    tap_due,
    tick,
    wake_pond,
)
from .core import (
    NEVER,
    ZERO,
    BeginRun,
    Pond,
    PondState,
    Refresh,
    Ripple,
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
    "Refresh",
    "Ripple",
    "RippleState",
    "ThrottleState",
    "Window",
    "EngineState",
    "min_demand",
    "max_demand",
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
    "block_on_missing_asset",
    "clear_missing_asset",
    "refresh_now",
    "refresh_next_due",
    "record_persist",
    "sleep_pond",
    "tap_due",
    "wake_pond",
    "force_pond",
    "rebuild_pond",
    "repair_pond",
    "kill_pond",
    "clear_pond",
    "restore_slots",
    "sentinel",
    "tick",
    "next_wake",
    "drain_begin_runs",
    "drain_passes",
    "pond",
    "worker",
]
