"""Scan (order-dependent) metric specs for the Trickle builder's ``.accumulate(by=…, name=acc.…)`` operator.

Where :mod:`duckstring.agg` reductions are **order-independent** (one value per group), an ``acc.*`` scan is
**order-dependent** and **per-row**: it enriches *every* row with its running value computed in the
``.along(...)`` order within its ``by`` group (``cumsum`` up to this row, the current ``ema``, …). The output
has the same cardinality as the input — `.accumulate()` is a transform, not a reduction — and is written by a
following **`.append()`** (the running values are only sound while history is tail-only, so a merge/retraction
would invalidate them; see ``plans/trickle-agg.md``).

Maintained incrementally by a per-group **carried fold-state** (``_duckstring_acc_{name}`` companion) that the
next run continues from the tail — ``O(new rows)`` per run. The ``.along`` axis **must be non-decreasing with
freshness** (a row arriving below its group's high-water mark breaks the scan and raises).

Supported:

- ``cumsum(col)`` — running sum.
- ``running_count()`` — running row count (1, 2, 3, …).
- ``running_min(col)`` / ``running_max(col)`` — running extreme so far.
- ``ema(col, alpha)`` — discrete exponential moving average ``α·x + (1−α)·ema_prev`` (each row one step).
- ``time_decayed_ema(col, lam)`` — continuous EMA whose decay scales with the **gap** in the ``.along`` value:
  ``α_t = 1 − exp(−lam·Δt)`` (so ``.along`` must be numeric — e.g. an epoch). The first row in a group is the
  seed (``α_t = 1``).

Usage::

    from duckstring import acc
    (pond.trickle("orders.order_line")
         .along("event_time")
         .accumulate(by="product_id",
                     run_total=acc.cumsum("qty"),
                     smoothed=acc.ema("unit_price", 0.3),
                     decayed=acc.time_decayed_ema("unit_price", lam=0.001))
         .append("order_line_scored", pk="order_id"))
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccMetric:
    """One scan output: its ``kind``, the input column it reads (if any), and the scalar parameter ``param``
    (``alpha`` for ema / ``lam`` for time_decayed_ema)."""

    kind: str
    col: str | None = None
    param: float | None = None


def cumsum(col: str) -> AccMetric:
    """Running sum of ``col`` along the axis (NULLs contribute 0)."""
    return AccMetric("cumsum", col)


def running_count() -> AccMetric:
    """Running count of rows seen so far in the group (1, 2, 3, …)."""
    return AccMetric("running_count")


def running_min(col: str) -> AccMetric:
    """Running minimum of ``col`` seen so far (NULLs ignored)."""
    return AccMetric("running_min", col)


def running_max(col: str) -> AccMetric:
    """Running maximum of ``col`` seen so far (NULLs ignored)."""
    return AccMetric("running_max", col)


def ema(col: str, alpha: float) -> AccMetric:
    """Discrete exponential moving average of ``col`` — ``α·x + (1−α)·ema_prev`` per row, ``0 < α ≤ 1``."""
    if not 0 < alpha <= 1:
        raise ValueError(f"ema(alpha={alpha!r}): need 0 < alpha <= 1")
    return AccMetric("ema", col, float(alpha))


def time_decayed_ema(col: str, lam: float) -> AccMetric:
    """Continuous (time-decayed) EMA of ``col`` — the decay scales with the gap ``Δt`` in the ``.along`` value
    (which must be numeric): ``α_t = 1 − exp(−lam·Δt)``, ``lam > 0``."""
    if lam <= 0:
        raise ValueError(f"time_decayed_ema(lam={lam!r}): need lam > 0")
    return AccMetric("time_decayed_ema", col, float(lam))
