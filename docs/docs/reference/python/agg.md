---
title: agg
description: Aggregation metrics for .aggregate().
---

# agg

```python
from duckstring import agg
```

Metric specs for the builder's [`.aggregate()`](trickle_builder.md#aggregate-and-group_by). Each function returns a spec, which you pass as a keyword argument naming the output column:

```python
.aggregate(by="product_id", total_revenue=agg.sum("revenue"), orders=agg.count())
```

All metrics are maintained incrementally. For most, a change costs time proportional to the changed rows. The ones marked "rescan" also extend cheaply when rows are added, but when a row is removed from a group, that group is recomputed from its current rows.

NULLs are ignored unless stated otherwise, so a group with only NULL inputs produces NULL.

## Counts and sums

| Function | Result |
|---|---|
| `count()` | Number of rows in the group, `count(*)`. |
| `sum(col)` | Sum of `col`. |
| `mean(col)` | Mean of `col`, maintained as a sum and a count. |
| `product(col)` | Product of `col`. A group containing a zero is `0`. Returned as `DOUBLE`, so large integer products aren't exact. |

## Spread

| Function | Result |
|---|---|
| `var(col, how="sample")` | Variance of `col`. |
| `stddev(col, how="sample")` | Standard deviation of `col`. |

`how` is `"sample"` (divide by `n - 1`, NULL for fewer than two rows) or `"pop"` (divide by `n`). Both are maintained in a numerically stable form that doesn't lose precision as values are added and removed.

## Weighted

| Function | Result |
|---|---|
| `weight_total(w)` | Sum of the weights, Σw. |
| `weighted_sum(x, w)` | Σ(w·x), over rows where both are non-NULL. |
| `weighted_average(x, w)` | Σ(w·x) / Σw, over rows where both are non-NULL. NULL when Σw is 0. |

## Two variables

These use only rows where both columns are non-NULL.

| Function | Result |
|---|---|
| `covariance(x, y, how="sample")` | Covariance of `x` and `y`. `how` as for `var`. |
| `pearson_correlation(x, y)` | Pearson correlation. NULL for fewer than two rows or when either column has no spread. |
| `ols_slope(x, y)` | Least-squares slope of `y` on `x`. NULL when `x` has no spread. |
| `ols_intercept(x, y)` | Least-squares intercept of `y` on `x`. NULL when `x` has no spread. |

## Extremes and logical reductions

These rescan a group when one of its rows is removed.

| Function | Result |
|---|---|
| `min(col)` | Minimum of `col`. |
| `max(col)` | Maximum of `col`. |
| `argmin(arg, by)` | The value of `arg` on the row where `by` is smallest. Ties are resolved arbitrarily. |
| `argmax(arg, by)` | The value of `arg` on the row where `by` is largest. Ties are resolved arbitrarily. |
| `bool_and(col)` | Logical AND of `col`. |
| `bool_or(col)` | Logical OR of `col`. |
| `bit_and(col)` | Bitwise AND of an integer `col`. |
| `bit_or(col)` | Bitwise OR of an integer `col`. |

## Custom reduction

### `reduce`

```python
agg.reduce(fn, init, *, inverse=None, dtype="DOUBLE")
```

A custom reduction that depends on row order: it folds each group's rows in [`.along()`](trickle_builder.md#along) order and produces one value per group.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `fn` | callable | | `fn(state, row) -> (new_state, output)`, where `row` is a `{column: value}` dict. The group's result is the `output` of its last row. |
| `init` | any | | The starting state for each group. |
| `inverse` | callable | `None` | Reserved for a future optimisation; not used. |
| `dtype` | `str` | `"DOUBLE"` | The DuckDB type of the output column. |

Requires `.along()` before `.aggregate()`, can't share an `.aggregate()` call with other metrics, and must be finished with `.merge()`. Any change to a group recomputes it from its current rows.

```python
def last_price(state, row):
    return row["unit_price"], row["unit_price"]

(
    pond.trickle("catalog.price_history")
    .along("changed_at")
    .aggregate(by="product_id", latest_price=agg.reduce(last_price, None))
    .merge("latest_price")
)
```

For holistic aggregates such as medians, percentiles and distinct counts, use [`.sql()`](trickle_builder.md#sql).
