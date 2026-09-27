---
title: Aggregation and Accumulation
description: Maintain grouped totals and running values incrementally.
---

# Aggregation and Accumulation

The demo `revenue` Pond re-aggregates every priced order line on every run, which costs more as history grows. The builder can instead maintain an aggregation from the changes alone: a new order line adjusts its product's totals, and a price change adjusts the products it touched. This guide covers aggregations, which reduce each group to one row, and accumulations, which add a running value to every row. It builds on [Joins with the Builder](joins_with_the_builder.md).

## Aggregating

`.aggregate()` groups the result so far and maintains one row per group. Each metric is an [`agg`](../reference/python/agg.md) spec, named by its keyword:

```python
from duckstring import agg, ripple


@ripple
def by_product(pond):
    (
        pond.trickle("priced.priced_line")
        .aggregate(by="product_id",
                   total_revenue=agg.sum("revenue"),
                   units_sold=agg.sum("quantity"),
                   order_count=agg.count())
        .merge("revenue_by_product")
    )
```

This is the demo `revenue` Pond rewritten to be incremental. The output's primary key is the grouping columns, so `.merge()` needs no `pk`. `.group_by("product_id").aggregate(...)` is the same thing, if you prefer that form.

An aggregation can follow joins, filters and computed columns:

```python
(
    pond.trickle("orders.order_line")
    .join(pond.trickle("catalog.product"), on="product_id")
    .select("s0.order_id, s1.category, s0.quantity * s1.unit_price AS revenue")
    .aggregate(by="category",
               total=agg.sum("revenue"),
               largest_order=agg.max("revenue"),
               mean_order=agg.mean("revenue"))
    .merge("category_revenue")
)
```

When a product's price changes, its order lines are recomputed by the join, and their old and new revenues flow into the aggregation as changes to the affected categories. Nothing else is recomputed.

`.aggregate()` must be the last step before `.merge()`. To work further with the totals, read them in a downstream Ripple or Pond.

### Choosing metrics

Most metrics update from changes alone: counts and sums, means, variances and standard deviations, weighted sums and averages, covariance, correlation and least-squares fits. Variances and related statistics are maintained in a form that stays numerically accurate as rows are added and removed.

A few metrics extend cheaply when rows arrive but must recompute a group from its current rows when one of its rows is removed: `min`, `max`, `argmin`, `argmax`, and the logical and bitwise reductions. On append-only input they never need to. On input with frequent deletions or updates, a group touched by a removal costs as much as that group's size.

Medians, percentiles and distinct counts need the whole group every time. Compute them with [`.sql()`](joins_with_the_builder.md#anything-else-sql), which recomputes in full but still writes only changed rows.

## Accumulating

An accumulation adds a running value to every row: a running total, a moving average, the previous row's value. It needs an order, given by `.along()`, and optionally groups, given by `by`:

```python
from duckstring import acc

(
    pond.trickle("orders.order_line")
    .along("ordered_at")
    .accumulate(by="store_id",
                running_units=acc.sum("quantity"),
                smoothed_quantity=acc.ema("quantity", alpha=0.5),
                previous_quantity=acc.prev("quantity"))
    .append("store_running", pk="order_id")
)
```

Each order line gets its store's running unit count, an exponential moving average of quantity, and the quantity of the store's previous order. The output has one row per input row.

Each group's running state is carried from one run to the next, so a new order continues from where its store left off rather than re-scanning the store's history.

### Append or merge

How you finish depends on how the input changes:

| Input | Finish with | Cost per run |
|---|---|---|
| Rows are only added, arriving in `.along()` order: each run's rows come after the previous run's within each group. | `.append()` | Proportional to the new rows. |
| Rows can be updated or removed, or arrive out of order. | `.merge(pk=...)` | New rows at the end of a group continue from carried state; a group with an earlier change is recomputed. |

`.append()` raises if a row arrives earlier than its group's latest, or a row is removed, since an append table can't rewrite the running values after it. The demo `orders` Pond generates order dates at random, so it would need `.merge()`.

### Custom running values

`acc.scan` runs your own function over each group's rows in order. It receives the state carried from the previous row and the current row, and returns the new state and the row's value:

```python
def big_order_streak(state, row):
    streak = state + 1 if row["quantity"] > 1 else 0
    return streak, streak


(
    pond.trickle("orders.order_line")
    .along("ordered_at")
    .accumulate(by="store_id", streak=acc.scan(big_order_streak, 0, dtype="INTEGER"))
    .append("store_streak", pk="order_id")
)
```

The state is stored as JSON between runs, so keep it to numbers, strings, lists and dicts. DECIMAL columns arrive as `decimal.Decimal`; convert them with `float()` before keeping them in the state.

## Order-dependent totals

Some per-group values depend on order but produce one value per group, such as the most recent price. `agg.reduce` folds each group's rows in `.along()` order and keeps the last output:

```python
def latest(state, row):
    price = float(row["unit_price"])
    return price, price


(
    pond.trickle("catalog.price_history")
    .along("changed_at")
    .aggregate(by="product_id", latest_price=agg.reduce(latest, None))
    .merge("latest_price")
)
```

Any change to a group recomputes that group, so a removed price-history row correctly reverts the product to its previous price. `agg.reduce` can't share an `.aggregate()` call with other metrics.
