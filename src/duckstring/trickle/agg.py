"""Aggregation metrics for the Trickle builder's ``.aggregate()``.

Each function returns a spec, passed as a keyword argument naming the output column::

    from duckstring import agg
    (pond.trickle("priced.priced_line")
         .aggregate(by="product_id",
                    total_revenue=agg.sum("revenue"),
                    orders=agg.count(),
                    revenue_sd=agg.stddev("revenue"))
         .merge("revenue_by_product"))   # pk defaults to `by`

Every metric is maintained incrementally. ``min``, ``max``, ``argmin``, ``argmax``, ``bool_and``,
``bool_or``, ``bit_and`` and ``bit_or`` extend cheaply as rows are added, but recompute a group from its
current rows when one of its rows is removed. NULLs are ignored unless stated otherwise.

Reference: https://docs.duckstring.com/reference/python/agg
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    """An aggregation spec, created by the functions in this module and passed to ``.aggregate()``."""

    kind: str
    col: str | None = None
    how: str | None = None
    col2: str | None = None
    ordered: bool = False
    fn: object = None       # the reducer for agg.reduce — fn(state, row) -> (new_state, output)
    init: object = None     # its per-group initial state
    dtype: str | None = None


def count() -> Metric:
    """Number of rows in the group, ``count(*)``."""
    return Metric("count")


def sum(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Sum of ``col``. A group with only NULLs is NULL."""
    return Metric("sum", col)


def mean(col: str) -> Metric:
    """Mean of ``col``, maintained as a sum and a count."""
    return Metric("mean", col)


def min(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Minimum of ``col``. Removing a row recomputes the group."""
    return Metric("min", col)


def max(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Maximum of ``col``. Removing a row recomputes the group."""
    return Metric("max", col)


def var(col: str, how: str = "sample") -> Metric:
    """Variance of ``col``. ``how`` is ``"sample"`` (divide by n - 1; the default) or ``"pop"`` (divide by n)."""
    return Metric("var", col, _check_how(how))


def stddev(col: str, how: str = "sample") -> Metric:
    """Standard deviation of ``col``. ``how`` is ``"sample"`` (the default) or ``"pop"``."""
    return Metric("stddev", col, _check_how(how))


def product(col: str) -> Metric:
    """Product of ``col``. A group containing a zero is 0. Returned as DOUBLE, so large integer products
    aren't exact.
    """
    return Metric("product", col)


# ─── weighted (additive — pure Σ, trivially retractable) ─────────────────────────


def weight_total(w: str) -> Metric:
    """Sum of the weights ``w``."""
    return Metric("weight_total", w)


def weighted_sum(x: str, w: str) -> Metric:
    """Sum of ``w * x`` over rows where both are non-NULL."""
    return Metric("weighted_sum", x, col2=w)


def weighted_average(x: str, w: str) -> Metric:
    """Sum of ``w * x`` divided by the sum of ``w``, over rows where both are non-NULL. NULL when the weights sum to 0."""
    return Metric("weighted_average", x, col2=w)


# ─── two-variable co-moments (paired; maintained by the parallel Pébay merge) ─────
#
# All four read the paired accumulator ``(n, Σx, Σy, M2x, M2y, Cxy)`` over rows where *both* columns are
# non-NULL (pairwise deletion). The centred sums are maintained well-conditioned (never Σxy − ΣxΣy/n); see
# ``trickle/io.py`` and ``plans/trickle-agg.md``.


def covariance(x: str, y: str, how: str = "sample") -> Metric:
    """Covariance of ``x`` and ``y`` over rows where both are non-NULL. ``how`` is ``"sample"`` (the default) or ``"pop"``."""
    return Metric("covariance", x, _check_how(how), col2=y)


def pearson_correlation(x: str, y: str) -> Metric:
    """Pearson correlation of ``x`` and ``y``. NULL for fewer than two rows or when either has no spread."""
    return Metric("pearson_correlation", x, col2=y)


def ols_slope(x: str, y: str) -> Metric:
    """Least-squares slope of ``y`` on ``x``. NULL when ``x`` has no spread."""
    return Metric("ols_slope", x, col2=y)


def ols_intercept(x: str, y: str) -> Metric:
    """Least-squares intercept of ``y`` on ``x``. NULL when ``x`` has no spread."""
    return Metric("ols_intercept", x, col2=y)


def reduce(fn, init, *, inverse=None, dtype: str = "DOUBLE") -> Metric:  # noqa: A001 - the reduce primitive
    """A custom reduction that depends on row order, producing one value per group.

    Args:
        fn: ``fn(state, row) -> (new_state, output)``, where ``row`` is a ``{column: value}`` dict. The
            group's result is the ``output`` of its last row, in ``.along()`` order.
        init: Each group's starting state.
        inverse: Reserved for a future optimisation; not used.
        dtype: The DuckDB type of the output column.

    Needs ``.along()`` before ``.aggregate()``, can't share an ``.aggregate()`` with other metrics, and must
    be finished with ``.merge()``. Any change to a group recomputes it.
    """
    return Metric("reduce", fn=fn, init=init, dtype=dtype)


# ─── payload extremes & semigroup reductions (rescan a group on a retraction) ─────


def argmin(arg: str, by: str) -> Metric:
    """The value of ``arg`` on the row where ``by`` is smallest. Ties resolve arbitrarily. Removing a row
    recomputes the group."""
    return Metric("argmin", arg, col2=by)


def argmax(arg: str, by: str) -> Metric:
    """The value of ``arg`` on the row where ``by`` is largest. Ties resolve arbitrarily. Removing a row
    recomputes the group."""
    return Metric("argmax", arg, col2=by)


def bool_and(col: str) -> Metric:
    """Logical AND of ``col``. Removing a row recomputes the group."""
    return Metric("bool_and", col)


def bool_or(col: str) -> Metric:
    """Logical OR of ``col``. Removing a row recomputes the group."""
    return Metric("bool_or", col)


def bit_and(col: str) -> Metric:
    """Bitwise AND of an integer ``col``. Removing a row recomputes the group."""
    return Metric("bit_and", col)


def bit_or(col: str) -> Metric:
    """Bitwise OR of an integer ``col``. Removing a row recomputes the group."""
    return Metric("bit_or", col)


def _check_how(how: str) -> str:
    h = how.lower()
    if h in ("pop", "population"):
        return "pop"
    if h == "sample":
        return "sample"
    raise ValueError(f"agg how={how!r}: one of 'sample' / 'pop'")
