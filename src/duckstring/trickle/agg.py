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
- **Weighted** (additive — pure sums, trivially retractable):
  - ``weight_total(w)`` — ``Σw``; ``weighted_sum(x, w)`` — ``Σ(w·x)``; ``weighted_average(x, w)`` —
    ``Σ(w·x) / Σw``.
- **Two-variable co-moments** (paired ``(n, Σx, Σy, M2x, M2y, Cxy)`` over rows where both are non-NULL,
  maintained well-conditioned by the same Pébay merge):
  - ``covariance(x, y, how)`` — ``Cxy/(n-1)`` (sample) / ``Cxy/n`` (pop).
  - ``pearson_correlation(x, y)`` — ``Cxy / sqrt(M2x·M2y)``.
  - ``ols_slope(x, y)`` — ``Cxy/M2x``; ``ols_intercept(x, y)`` — ``ȳ − slope·x̄``.

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
    correlation, weighted, OLS); ``ordered`` flags an order-dependent metric (the ``acc.*`` scans),
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


# ─── weighted (additive — pure Σ, trivially retractable) ─────────────────────────


def weight_total(w: str) -> Metric:
    """Sum of the weights ``Σw`` over rows where ``w`` is non-NULL."""
    return Metric("weight_total", w)


def weighted_sum(x: str, w: str) -> Metric:
    """Weighted sum ``Σ(w·x)`` over rows where both ``x`` and ``w`` are non-NULL."""
    return Metric("weighted_sum", x, col2=w)


def weighted_average(x: str, w: str) -> Metric:
    """Weighted mean ``Σ(w·x) / Σw`` over rows where both ``x`` and ``w`` are non-NULL (NULL if ``Σw`` = 0)."""
    return Metric("weighted_average", x, col2=w)


# ─── two-variable co-moments (paired; maintained by the parallel Pébay merge) ─────
#
# All four read the paired accumulator ``(n, Σx, Σy, M2x, M2y, Cxy)`` over rows where *both* columns are
# non-NULL (pairwise deletion). The centred sums are maintained well-conditioned (never Σxy − ΣxΣy/n); see
# ``trickle/io.py`` and ``plans/trickle-agg.md``.


def covariance(x: str, y: str, how: str = "sample") -> Metric:
    """Covariance of ``x`` and ``y`` — ``Cxy / (n-1)`` (sample, default; NULL for n<2) or ``Cxy / n`` (pop)."""
    return Metric("covariance", x, _check_how(how), col2=y)


def pearson_correlation(x: str, y: str) -> Metric:
    """Pearson correlation ``Cxy / sqrt(M2x · M2y)`` (NULL when n<2 or either spread is 0)."""
    return Metric("pearson_correlation", x, col2=y)


def ols_slope(x: str, y: str) -> Metric:
    """Ordinary-least-squares slope of ``y`` on ``x`` — ``Cxy / M2x`` (NULL when ``x`` has no spread)."""
    return Metric("ols_slope", x, col2=y)


def ols_intercept(x: str, y: str) -> Metric:
    """Ordinary-least-squares intercept of ``y`` on ``x`` — ``ȳ − slope·x̄`` (NULL when ``x`` has no spread)."""
    return Metric("ols_intercept", x, col2=y)


# ─── payload extremes & semigroup reductions (rescan a group on a retraction) ─────


def argmin(arg: str, by: str) -> Metric:
    """The ``arg`` value at the row where ``by`` is **minimal** (DuckDB ``arg_min``; ties resolved
    arbitrarily). A retraction of the supporting row rescans the group."""
    return Metric("argmin", arg, col2=by)


def argmax(arg: str, by: str) -> Metric:
    """The ``arg`` value at the row where ``by`` is **maximal** (DuckDB ``arg_max``; ties resolved
    arbitrarily). A retraction of the supporting row rescans the group."""
    return Metric("argmax", arg, col2=by)


def bool_and(col: str) -> Metric:
    """Logical AND over ``col`` (NULLs ignored). A retraction rescans the group."""
    return Metric("bool_and", col)


def bool_or(col: str) -> Metric:
    """Logical OR over ``col`` (NULLs ignored). A retraction rescans the group."""
    return Metric("bool_or", col)


def bit_and(col: str) -> Metric:
    """Bitwise AND over an integer ``col`` (NULLs ignored). A retraction rescans the group."""
    return Metric("bit_and", col)


def bit_or(col: str) -> Metric:
    """Bitwise OR over an integer ``col`` (NULLs ignored). A retraction rescans the group."""
    return Metric("bit_or", col)


def _check_how(how: str) -> str:
    h = how.lower()
    if h in ("pop", "population"):
        return "pop"
    if h == "sample":
        return "sample"
    raise ValueError(f"agg how={how!r}: one of 'sample' / 'pop'")
