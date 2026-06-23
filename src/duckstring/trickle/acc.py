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
- ``product(col)`` — running product (float output; a 0 makes it stay 0).
- ``prev(col)`` / ``lag(col, n)`` — the value ``n`` rows back (a length-``n`` FIFO buffer of carried state,
  so it reaches across run boundaries).
- ``convolution(col, kernel)`` — a 1-D FIR filter over the last ``len(kernel)`` values.
- ``ema(col, alpha)`` — discrete exponential moving average ``α·x + (1−α)·ema_prev`` (each row one step).
- ``tema(col, lam)`` — time-decayed (continuous) EMA whose decay scales with the **gap** in the ``.along``
  value: ``α_t = 1 − exp(−lam·Δt)`` (so ``.along`` must be numeric — e.g. an epoch). The first row in a group
  is the seed (``α_t = 1``).
- ``scan(fn, init, dtype)`` — a custom fold ``fn(state, row) -> (new_state, output)`` (JSON-serializable
  state).

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
    """One scan output: its ``kind``, the input column it reads (if any), the scalar parameter ``param``
    (``alpha`` for ema / ``lam`` for tema), and — for the custom :func:`scan` fold — the reducer ``fn``, its
    ``init`` state, and the output ``dtype``."""

    kind: str
    col: str | None = None
    param: float | None = None
    fn: object = None
    init: object = None
    dtype: str | None = None


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


def product(col: str) -> AccMetric:  # noqa: A001 - mirrors agg.product on the scan namespace
    """Running product of ``col`` along the axis (NULLs ignored; the first non-NULL seeds it; once a 0 is
    seen the running product stays 0). Output is a float (DOUBLE) — large products overflow to ±inf."""
    return AccMetric("product", col)


def prev(col: str) -> AccMetric:
    """The value of ``col`` one row back in the group (``lag`` 1) — NULL on the first row. ``prev`` reaches
    across the run boundary into the previous run's tail (the one-slot buffer is carried state)."""
    return AccMetric("lag", col, 1)


def lag(col: str, n: int = 1) -> AccMetric:
    """The value of ``col`` ``n`` rows back in the group (NULL until the group has ``n`` prior rows). Carried
    as a length-``n`` FIFO buffer, so it reaches back across run boundaries."""
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"lag(n={n!r}): need a positive integer")
    return AccMetric("lag", col, n)


def convolution(col: str, kernel) -> AccMetric:
    """A 1-D convolution / FIR filter: the dot product of ``kernel`` with the last ``len(kernel)`` values of
    ``col`` (oldest·``kernel[0]`` … current·``kernel[-1]``), in ``.along`` order — NULL until the group has
    ``len(kernel)`` rows; NULL inputs count as 0. Carried as a length-``K`` FIFO buffer; output is a float."""
    kernel = tuple(kernel)
    if not kernel:
        raise ValueError("convolution(kernel=...): the kernel must be non-empty")
    return AccMetric("conv", col, init=kernel)


def scan(fn, init, dtype: str = "DOUBLE") -> AccMetric:
    """A **custom fold**: ``fn(state, row) -> (new_state, output)`` applied per row in ``.along`` order, with
    ``init`` the per-group starting state and ``row`` a ``{column: value}`` dict of that row's output columns.
    ``output`` (the per-row value, ``dtype``) is appended; ``new_state`` is carried to the next row. The state
    is persisted between runs as **JSON**, so it must be JSON-serializable (prefer lists/dicts/numbers — a
    tuple round-trips to a list); ``output`` must be a scalar compatible with ``dtype``."""
    return AccMetric("scan", None, None, fn=fn, init=init, dtype=dtype)
