"""Aggregate metric specs for the Trickle builder's ``.aggregate(by=…, name=agg.…)`` operator.

Each metric is a small typed spec, not a SQL string, because the incremental engine needs to know its
*kind* — which accumulators it maintains and how it updates from a delta. The supported set:

- **Distributive** — maintainable from the delta alone:
  - ``count()`` — the group's row count.
  - ``sum(col)`` — running sum (SQL semantics: NULLs ignored; an all-NULL group sums to NULL).
  - ``min(col)`` / ``max(col)`` — the extreme; an insert extends it in place (O(δ)), but a retraction of
    the supporting row triggers a **rescan** of the group's current membership.
- **Algebraic** — derived from distributive accumulators:
  - ``mean(col)`` — ``sum(col) / count(col)`` over non-NULL values.
  - ``var(col)`` / ``stddev(col)`` — from ``count`` and the **centred second moment** ``M2 = Σ(x − x̄)²``,
    maintained by the parallel (Chan/Pébay) merge-in/merge-out form (retractable *and* well-conditioned —
    never the cancellation-prone ``Σx² − (Σx)²/n``; see ``plans/trickle-agg.md``). ``how`` ∈ ``"sample"``
    (default, matching Ibis; ``/(n-1)``, NULL for n<2) or ``"pop"`` (``/n``).

``min``/``max`` need the current group membership on a retraction; everything else is pure ``O(δ)``.

Usage::

    from duckstring import agg
    (pond.trickle("priced.priced_line")
         .aggregate(by="product_id",
                    total_revenue=agg.sum("revenue"),
                    orders=agg.count(),
                    top_price=agg.max("unit_price"),
                    revenue_sd=agg.stddev("revenue"))
         .merge("revenue_by_product"))   # pk defaults to `by`
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    """One output aggregate: its ``kind``, the input column it reads (if any), and — for ``var``/``stddev``
    — whether it's a ``sample`` or ``pop``ulation statistic.

    ``col2`` carries a second input column for the two-variable metrics landing in Phase 1 (covariance,
    correlation, weighted, OLS); ``ordered`` flags an order-dependent metric (cumsum / ema / running_*),
    which the builder will only accept on ``.append()`` and only with an ``.along(...)`` axis declared.
    Both default to the single-variable, order-independent case and are not yet consumed — they are the
    foundation the later phases build on (see ``plans/trickle-agg.md``)."""

    kind: str
    col: str | None = None
    how: str | None = None
    col2: str | None = None
    ordered: bool = False


def count() -> Metric:
    """Count of rows in the group (``count(*)``)."""
    return Metric("count")


def sum(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Running sum of ``col`` (NULLs ignored; an all-NULL group is NULL, per SQL ``sum``)."""
    return Metric("sum", col)


def mean(col: str) -> Metric:
    """Mean of ``col`` over its non-NULL values — algebraic, maintained as ``sum(col)/count(col)``."""
    return Metric("mean", col)


def min(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Minimum of ``col`` (NULLs ignored). A retraction of the supporting row rescans the group."""
    return Metric("min", col)


def max(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Maximum of ``col`` (NULLs ignored). A retraction of the supporting row rescans the group."""
    return Metric("max", col)


def var(col: str, how: str = "sample") -> Metric:
    """Variance of ``col`` over its non-NULL values. ``how`` ∈ ``"sample"`` (default) / ``"pop"``."""
    return Metric("var", col, _check_how(how))


def stddev(col: str, how: str = "sample") -> Metric:
    """Standard deviation of ``col`` over its non-NULL values. ``how`` ∈ ``"sample"`` (default) / ``"pop"``."""
    return Metric("stddev", col, _check_how(how))


def _check_how(how: str) -> str:
    h = how.lower()
    if h in ("pop", "population"):
        return "pop"
    if h == "sample":
        return "sample"
    raise ValueError(f"agg how={how!r}: one of 'sample' / 'pop'")
