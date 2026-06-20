"""Aggregate metric specs for the Trickle builder's ``.aggregate(by=…, name=agg.…)`` operator.

Each metric is a small typed spec, not a SQL string, because the incremental engine needs to know its
*kind* — which accumulators it maintains and how it updates from a delta. This first cut is the
**distributive / algebraic** set, all maintainable from the delta alone (no rescan):

- ``count()`` — the group's row count.
- ``sum(col)`` — running sum (SQL semantics: NULLs ignored; an all-NULL group sums to NULL).
- ``mean(col)`` — algebraic: ``sum(col) / count(col)`` over non-NULL values.

``min`` / ``max`` (retraction-aware, need a rescan) and ``var`` / ``stddev`` are a later cut — see
``plans/trickle-relational.md`` §5–6.

Usage::

    from duckstring import agg
    (pond.trickle("priced.priced_line")
         .aggregate(by="product_id",
                    total_revenue=agg.sum("revenue"),
                    orders=agg.count(),
                    avg_revenue=agg.mean("revenue"))
         .merge("revenue_by_product"))   # pk defaults to `by`
"""

from __future__ import annotations

from dataclasses import dataclass

_DISTRIBUTIVE = {"count", "sum", "mean"}


@dataclass(frozen=True)
class Metric:
    """One output aggregate: its ``kind`` and (for column metrics) the input column it reads."""

    kind: str
    col: str | None = None


def count() -> Metric:
    """Count of rows in the group (``count(*)``)."""
    return Metric("count")


def sum(col: str) -> Metric:  # noqa: A001 - deliberate SQL-style name on the agg namespace
    """Running sum of ``col`` (NULLs ignored; an all-NULL group is NULL, per SQL ``sum``)."""
    return Metric("sum", col)


def mean(col: str) -> Metric:
    """Mean of ``col`` over its non-NULL values — algebraic, maintained as ``sum(col)/count(col)``."""
    return Metric("mean", col)
