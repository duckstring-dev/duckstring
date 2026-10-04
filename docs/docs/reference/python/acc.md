---
title: acc
description: Running metrics for .accumulate().
---

# acc

```python
from duckstring import acc
```

Metric specs for the builder's [`.accumulate()`](trickle_builder.md#accumulate). Each adds a running value to every row, computed in [`.along()`](trickle_builder.md#along) order within the row's group. Pass each spec as a keyword argument naming the output column:

```python
.along("ordered_at").accumulate(by="store_id", running_units=acc.sum("quantity"))
```

Each group's running state is carried between runs, so new rows continue from where the group left off, including metrics that look back several rows.

## Running aggregates

| Function | Result on each row |
|---|---|
| `sum(col)` | Sum of `col` up to and including this row. NULLs count as 0. |
| `count()` | Number of rows so far: 1, 2, 3, ... |
| `min(col)` | Smallest `col` so far. NULLs are ignored. |
| `max(col)` | Largest `col` so far. NULLs are ignored. |
| `first(col)` | The first non-NULL `col` in the group. |
| `product(col)` | Product of `col` so far, as `DOUBLE`. The first non-NULL value starts it, NULLs are ignored, and it stays 0 once a 0 is seen. |

## Looking back

| Function | Result on each row |
|---|---|
| `prev(col)` | `col` from the previous row in the group. NULL on the first row. |
| `lag(col, n=1)` | `col` from `n` rows back. NULL until the group has `n` earlier rows. `n` must be a positive integer. |
| `convolution(col, kernel)` | Dot product of `kernel` with the last `len(kernel)` values of `col`, oldest value first. NULL until the group has `len(kernel)` rows; NULL inputs count as 0. Returned as `DOUBLE`. |

## Smoothing

| Function | Result on each row |
|---|---|
| `ema(col, alpha)` | Exponential moving average, `alpha·x + (1 − alpha)·previous`. Requires `0 < alpha ≤ 1`. |
| `tema(col, lam)` | Time-decayed moving average whose weight depends on the gap since the previous row: `alpha = 1 − exp(−lam·Δt)`, where `Δt` is the difference in the `.along()` column, which must be numeric. The first row in a group is taken as-is. Requires `lam > 0`. |

## Custom scan

### `scan`

```python
acc.scan(fn, init, dtype="DOUBLE")
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `fn` | callable | | `fn(state, row) -> (new_state, output)`, called for each row in order. `row` is a `{column: value}` dict of the row's columns, and `output` becomes the row's value. |
| `init` | any | | Each group's starting state. |
| `dtype` | `str` | `"DOUBLE"` | The DuckDB type of the output column. `output` must be a scalar of a compatible type. |

The state is stored as JSON between runs, so it must be JSON-serialisable. Tuples come back as lists, and DECIMAL columns arrive as `decimal.Decimal`, which must be converted (for example with `float()`) before being kept in the state.

```python
def streak(state, row):
    n = state + 1 if row["quantity"] > 5 else 0
    return n, n

(
    pond.trickle("orders.order_line")
    .along("ordered_at")
    .accumulate(by="store_id", big_order_streak=acc.scan(streak, 0, dtype="INTEGER"))
    .merge("store_streaks", pk="order_id")
)
```

To reduce each group to a single value in order instead, use [`agg.reduce`](agg.md#reduce).
