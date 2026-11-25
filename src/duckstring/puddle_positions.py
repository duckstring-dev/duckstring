"""Where a Puddle Source's position goes next: one module for a local run (``pond run --incremental``) and a
Catchment's Puddle nodes (``refresh now --next-change``), so testing a Pond alone and on a dev Catchment move
through production's history the same way (plans/showcase-fixes.md item 10).

A Puddle's position is the freshness of its Source it is read at. A target ``T`` is a freshness to bring the
Pond to; every Puddle Source then moves to the earliest point of its history that reaches ``T``, and each is read
as it stood at the moment the last of them got there, as a Refresh at ``T`` would have seen them in production.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

TARGETS = ("next-change", "next-run", "most-recent")


def choose_target(kind: str, prev: datetime, now: datetime, *, sources: Iterable[str],
                  next_change: Callable[[str], datetime | None], own_runs: Callable[[], list[datetime] | None],
                  latest: Callable[[str], datetime], to: datetime | None = None,
                  no_runs: str = "--next-run needs this Pond's production runs, and none were found") -> datetime:
    """The freshness a Refresh from ``prev`` (the Pond's last Run) asks for.

    ``next-change``: the first change after ``prev`` among the Sources (``next_change``). ``next-run``: this
    Pond's next Run in production after ``prev`` (``own_runs``, ``None`` when unknown). ``most-recent``: the
    oldest of the Sources' newest points (``latest``), so every one of them can reach it. ``to``: that freshness.
    With nothing further, ``now`` (only steps can reach it)."""
    if to is not None:
        if to <= prev:
            raise ValueError(f"--to {to.isoformat()} isn't after the last run's freshness ({prev.isoformat()})")
        return to
    sources = list(sources)
    found: datetime | None = None
    if kind == "next-change":
        nexts = [c for s in sources if (c := next_change(s)) is not None]
        found = min(nexts) if nexts else None
    elif kind == "next-run":
        runs = own_runs()
        if runs is None:
            raise ValueError(no_runs)
        later = [r for r in runs if r > prev]
        found = min(later) if later else None
    elif kind == "most-recent":
        newest = [latest(s) for s in sources]
        found = min(newest) if newest else None
        if found is not None and found <= prev:
            found = None
    else:
        raise ValueError(f"unknown target '{kind}', one of: {', '.join(TARGETS)}")
    return found if found is not None else now


def reach(target: datetime, positions: dict[str, datetime], recorded: dict[str, list[datetime]], *,
          live: set[str], steppable: set[str] = frozenset()) -> tuple[dict[str, datetime], list[str]]:
    """Bring every Source to ``target``. ``positions`` is where each is now and ``recorded`` the points its history
    holds after its baseline. A Source reaches the target at its earliest point at or after it; each is then read
    at its newest point at or before the moment the last of them got there. A Source whose history falls short
    steps when it can (``steppable``: stamped at the target), and otherwise refuses, naming it (``live``:
    production hasn't got there yet). Returns ``({source: position}, stepping)``."""
    reached: dict[str, datetime] = {}
    stepping: list[str] = []
    for source, current in positions.items():
        points = [current, *recorded.get(source, ())]
        ahead = [p for p in points if p >= target]
        if ahead:
            reached[source] = min(ahead)
        elif source in steppable:
            stepping.append(source)
        elif source in live:
            raise ValueError(f"production hasn't brought '{source}' to {target.isoformat()} (its latest is "
                             f"{max(points).isoformat()}): pick an earlier target, or wait")
        else:
            raise ValueError(f"'{source}' has no history reaching {target.isoformat()} and no @puddle.step to go on "
                             "from it")
    moment = max([*reached.values(), target])
    out: dict[str, datetime] = {}
    for source in reached:
        out[source] = max(p for p in [positions[source], *recorded.get(source, ())] if p <= moment)
    for source in stepping:
        out[source] = target
    return out, stepping


__all__ = ["TARGETS", "choose_target", "reach"]
