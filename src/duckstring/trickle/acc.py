"""Scan (order-dependent) metric specs for the Trickle builder's ``.accumulate(by=…, name=acc.…)`` operator.

Where :mod:`duckstring.agg` reductions are **order-independent** (one value per group), an ``acc.*`` scan is
**order-dependent** and **per-row**: it enriches *every* row with its running value computed in the
``.along(...)`` order within its ``by`` group (``acc.sum`` up to this row, the current ``acc.ema``, …). The
output has the same cardinality as the input — `.accumulate()` is a transform, not a reduction — and is
written by a following **`.append()`** (the running values are only sound while history is tail-only, so a
merge/retraction would invalidate them; see ``plans/trickle-agg.md``). The ``acc.`` prefix is what marks a
metric as *accumulated*: ``acc.sum`` is a running sum, the counterpart of the order-independent ``agg.sum``.

Maintained incrementally by a per-group **carried fold-state** (``_duckstring_acc_{name}`` companion) that the
next run continues from the tail — ``O(new rows)`` per run. The ``.along`` axis **must be non-decreasing with
freshness** (a row arriving below its group's high-water mark breaks the scan and raises).

Supported:

- ``sum(col)`` — running sum.
- ``count()`` — running row count (1, 2, 3, …).
- ``min(col)`` / ``max(col)`` — running extreme so far.
- ``first(col)`` — the first non-NULL value seen in the group (frozen once set; emitted on every later row).
- ``ema(col, alpha)`` — discrete exponential moving average ``α·x + (1−α)·ema_prev`` (each row one step).
- ``tema(col, lam)`` — time-decayed (continuous) EMA whose decay scales with the **gap** in the ``.along``
  value: ``α_t = 1 − exp(−lam·Δt)`` (so ``.along`` must be numeric — e.g. an epoch). The first row in a group
  is the seed (``α_t = 1``).

Usage::

    from duckstring import acc
    (pond.trickle("orders.order_line")
         .along("event_time")
         .accumulate(by="product_id",
                     run_total=acc.sum("qty"),
                     smoothed=acc.ema("unit_price", 0.3),
                     decayed=acc.tema("unit_price", lam=0.001))
         .append("order_line_scored", pk="order_id"))
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccMetric:
    """One scan output: its ``kind``, the input column it reads (if any), and the scalar parameter ``param``
    (``alpha`` for ema / ``lam`` for tema)."""

    kind: str
    col: str | None = None
    param: float | None = None


def sum(col: str) -> AccMetric:  # noqa: A001 - deliberate SQL-style name on the acc namespace
    """Running sum of ``col`` along the axis (NULLs contribute 0)."""
    return AccMetric("sum", col)


def count() -> AccMetric:
    """Running count of rows seen so far in the group (1, 2, 3, …)."""
    return AccMetric("count")


def min(col: str) -> AccMetric:  # noqa: A001 - deliberate SQL-style name on the acc namespace
    """Running minimum of ``col`` seen so far (NULLs ignored)."""
    return AccMetric("min", col)


def max(col: str) -> AccMetric:  # noqa: A001 - deliberate SQL-style name on the acc namespace
    """Running maximum of ``col`` seen so far (NULLs ignored)."""
    return AccMetric("max", col)


def first(col: str) -> AccMetric:
    """The first non-NULL value of ``col`` in the group — frozen once set, emitted on every later row."""
    return AccMetric("first", col)


def ema(col: str, alpha: float) -> AccMetric:
    """Discrete exponential moving average of ``col`` — ``α·x + (1−α)·ema_prev`` per row, ``0 < α ≤ 1``."""
    if not 0 < alpha <= 1:
        raise ValueError(f"ema(alpha={alpha!r}): need 0 < alpha <= 1")
    return AccMetric("ema", col, float(alpha))


def tema(col: str, lam: float) -> AccMetric:
    """Time-decayed (continuous) EMA of ``col`` — the decay scales with the gap ``Δt`` in the ``.along`` value
    (which must be numeric): ``α_t = 1 − exp(−lam·Δt)``, ``lam > 0``."""
    if lam <= 0:
        raise ValueError(f"tema(lam={lam!r}): need lam > 0")
    return AccMetric("tema", col, float(lam))
