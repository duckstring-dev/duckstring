"""Running metrics for the Trickle builder's ``.accumulate()``.

Each adds a running value to every row, computed in ``.along()`` order within the row's ``by`` group.
Pass specs as keyword arguments naming the output columns::

    from duckstring import acc
    (pond.trickle("orders.order_line")
         .along("ordered_at")
         .accumulate(by="store_id",
                     running_units=acc.sum("quantity"),
                     smoothed=acc.ema("quantity", alpha=0.2))
         .merge("store_running_units", pk="order_id"))

Each group's running state is carried between runs, so new rows continue where the group left off.
Finish with ``.append()`` when rows only arrive in increasing ``.along()`` order, or ``.merge()`` to
handle removals and late rows.

Reference: https://docs.duckstring.com/reference/python/acc
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccMetric:
    """A running-metric spec, created by the functions in this module and passed to ``.accumulate()``."""

    kind: str
    col: str | None = None
    param: float | None = None
    fn: object = None
    init: object = None
    dtype: str | None = None


def sum(col: str) -> AccMetric:  # noqa: A001 - deliberate SQL-style name on the acc namespace
    """Sum of ``col`` up to and including this row. NULLs count as 0."""
    return AccMetric("sum", col)


def count() -> AccMetric:
    """Number of rows so far in the group: 1, 2, 3, ..."""
    return AccMetric("count")


def min(col: str) -> AccMetric:  # noqa: A001 - deliberate SQL-style name on the acc namespace
    """Smallest ``col`` so far. NULLs are ignored."""
    return AccMetric("min", col)


def max(col: str) -> AccMetric:  # noqa: A001 - deliberate SQL-style name on the acc namespace
    """Largest ``col`` so far. NULLs are ignored."""
    return AccMetric("max", col)


def first(col: str) -> AccMetric:
    """The first non-NULL ``col`` in the group."""
    return AccMetric("first", col)


def ema(col: str, alpha: float) -> AccMetric:
    """Exponential moving average of ``col``: ``alpha * x + (1 - alpha) * previous``. Requires ``0 < alpha <= 1``."""
    if not 0 < alpha <= 1:
        raise ValueError(f"ema(alpha={alpha!r}): need 0 < alpha <= 1")
    return AccMetric("ema", col, float(alpha))


def tema(col: str, lam: float) -> AccMetric:
    """Time-decayed moving average of ``col``, weighted by the gap since the previous row:
    ``alpha = 1 - exp(-lam * gap)``, where the gap is measured in the ``.along()`` column, which must be
    numeric. The first row in a group is taken as-is. Requires ``lam > 0``.
    """
    if lam <= 0:
        raise ValueError(f"tema(lam={lam!r}): need lam > 0")
    return AccMetric("tema", col, float(lam))


def product(col: str) -> AccMetric:  # noqa: A001 - mirrors agg.product on the scan namespace
    """Product of ``col`` so far, as DOUBLE. The first non-NULL value starts it, NULLs are ignored, and it
    stays 0 once a 0 is seen.
    """
    return AccMetric("product", col)


def prev(col: str) -> AccMetric:
    """``col`` from the previous row in the group. NULL on the first row."""
    return AccMetric("lag", col, 1)


def lag(col: str, n: int = 1) -> AccMetric:
    """``col`` from ``n`` rows back in the group. NULL until the group has ``n`` earlier rows. ``n`` must be
    a positive integer."""
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"lag(n={n!r}): need a positive integer")
    return AccMetric("lag", col, n)


def convolution(col: str, kernel) -> AccMetric:
    """Dot product of ``kernel`` with the last ``len(kernel)`` values of ``col``, oldest first. NULL until the
    group has ``len(kernel)`` rows; NULL inputs count as 0. Returned as DOUBLE.
    """
    kernel = tuple(kernel)
    if not kernel:
        raise ValueError("convolution(kernel=...): the kernel must be non-empty")
    return AccMetric("conv", col, init=kernel)


def scan(fn, init, dtype: str = "DOUBLE") -> AccMetric:
    """A custom running computation.

    Args:
        fn: ``fn(state, row) -> (new_state, output)``, called for each row in order. ``row`` is a
            ``{column: value}`` dict and ``output`` becomes the row's value.
        init: Each group's starting state.
        dtype: The DuckDB type of the output column.

    The state is stored as JSON between runs, so it must be JSON-serialisable; tuples come back as lists,
    and DECIMAL values (``decimal.Decimal``) must be converted, e.g. with ``float()``.
    """
    return AccMetric("scan", None, None, fn=fn, init=init, dtype=dtype)
