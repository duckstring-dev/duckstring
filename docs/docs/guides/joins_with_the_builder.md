---
title: Joins with the Builder
description: Join, filter and reshape Trickles incrementally.
---

# Joins with the Builder

The Trickle builder, `pond.trickle(...)`, composes joins and row-level transformations over other Ponds' tables and keeps the result up to date from their changes. When one product's price changes, it recomputes only the order lines for that product. This guide works through the common shapes. See [Trickles](../concepts/trickles.md) for how it works and [Trickle Builder](../reference/python/trickle_builder.md) for every option.

The examples use the demo Trickle Sources: `orders.order_line` (an append Trickle of order lines) and `catalog.product` (a merge Trickle of products), plus a small `stores.store` merge Trickle.

## A two-table join

This is the whole of the demo `priced` Pond:

```python
from duckstring import ripple


@ripple
def priced_line(pond):
    (
        pond.trickle("orders.order_line")
        .join(pond.trickle("catalog.product"), on="product_id")
        .select("s0.order_id, s0.product_id, s0.store_id, s0.quantity, s1.unit_price, "
                "round(s0.quantity * s1.unit_price, 2) AS revenue")
        .merge("priced_line", pk="order_id")
    )
```

The chain reads left to right: start from order lines, join products on `product_id`, choose the output columns, and write the result as the merge Trickle `priced_line`, keyed by `order_id`. Sources are named `s0`, `s1`, ... in the order they appear.

On the first run the builder computes the whole join. After that, each run reads the changes to both Sources since the last run: new orders are priced and added, and when a product's price changes, the order lines for that product are recomputed and their old versions removed. Everything else is left alone. Downstream, `priced_line` is itself a Trickle, so the next Pond can do the same.

## Naming sources

`s0` and `s1` get hard to follow as joins grow. Give sources names with `.alias()`:

```python
(
    pond.trickle("orders.order_line").alias("o")
    .join(pond.trickle("catalog.product").alias("p"), on="product_id")
    .select("o.order_id, o.quantity, p.unit_price, o.quantity * p.unit_price AS revenue")
    .merge("priced_line", pk="order_id")
)
```

## Choosing the output

There are three ways to shape the result, applied in the order you call them.

`.select()` sets the output columns, and can compute new ones. It must include the `pk`.

`.mutate()` adds computed columns and keeps all the existing ones. Without a `.select()`, the output is every column from every source, with the join key appearing once:

```python
(
    pond.trickle("orders.order_line").alias("o")
    .join(pond.trickle("catalog.product").alias("p"), on="product_id", how="left")
    .mutate(revenue="o.quantity * p.unit_price")
    .filter("revenue > 100")
    .merge("large_line", pk="order_id")
)
```

`.filter()` keeps matching rows. Because it comes after `.mutate()` here, it can use `revenue`.

If both sources have a column with the same name, apart from the join key, the full-column output is ambiguous and the builder raises an error. Use `.select()` to pick one.

Expressions must give the same answer every time for the same input. Removed rows cancel against earlier ones by matching every value, so `now()` or `random()` in an expression breaks incremental updates. Put the run's freshness in a column with a literal instead, if you need a timestamp.

## Join types and keys

`how` takes `inner` (the default), `left`, `right`, `full`, `semi` or `anti`, all maintained incrementally. `semi` and `anti` keep only left-side columns, which makes `anti` a natural way to find what's missing:

```python
(
    pond.trickle("catalog.product")
    .join(pond.trickle("orders.order_line"), on="product_id", how="anti")
    .merge("unsold_product", pk="product_id")
)
```

When a new product arrives with no orders, it appears in `unsold_product`; when its first order arrives, it's removed.

The join key can be any column, not just a primary key. For several columns, pass a list. For columns named differently on each side, pass a mapping:

```python
.join(pond.trickle("catalog.product"), on={"sku": "product_id"})
```

## More than two tables

Chain joins to build a star:

```python
(
    pond.trickle("orders.order_line").alias("o")
    .join(pond.trickle("catalog.product").alias("p"), on="product_id")
    .join(pond.trickle("stores.store").alias("s"), on="store_id")
    .select("o.order_id, o.quantity * p.unit_price AS revenue, p.category, s.region")
    .merge("sale_fact", pk="order_id")
)
```

The right-hand side of a join can itself be a join, which gives bushy and snowflake shapes. It can't carry its own `.filter()`, `.select()` or `.mutate()`; apply those to the combined result.

### Chaining through an intermediate table

A terminal returns a builder rooted at the table it just wrote, so a chain can carry on from it:

```python
(
    pond.trickle("orders.order_line")
    .join(pond.trickle("catalog.product"), on="product_id")
    .select("s0.order_id, s0.store_id, s0.quantity, s1.unit_price, "
            "round(s0.quantity * s1.unit_price, 2) AS revenue")
    .merge("priced_line", pk="order_id")
    .join(pond.trickle("stores.store"), on="store_id")
    .select("s0.order_id, s0.revenue, s1.region")
    .merge("regional_line", pk="order_id")
)
```

This saves `priced_line` as a table, so when only a store's region changes, the second join reuses it instead of recomputing the first join. It's also how to join on a column that only exists after an earlier join. Each step waits for the one before; split steps into separate Ripples if they can run in parallel.

## Append-only results

`.append()` writes to an append Trickle instead of a merge Trickle. Use it when output rows are only ever added. It changes what happens when an input changes: by default, a change that would alter an existing output row raises an error, since an append table can't rewrite history. With `fail_on_conflict=False`, such changes are ignored, which is exactly what you want to record a value as it was at the time:

```python
(
    pond.trickle("orders.order_line")
    .join(pond.trickle("catalog.product"), on="product_id")
    .select("s0.order_id, s0.quantity, s1.unit_price")
    .append("price_at_sale", pk="order_id", fail_on_conflict=False, log_drops=False)
)
```

When a product's price changes, existing rows keep the price they were sold at, and only new orders get the new price. With `log_drops=True` (the default), the ignored changes are recorded in a `price_at_sale__droplog` table for inspection.

With this combination, and the output keyed by the first source's own key, the builder only joins new order lines against the current products, rather than working out what a price change would have affected. That's much cheaper when the joined table changes often.

## Anything else: `.sql()`

The builder only offers operations it can update incrementally. For anything else, such as a window function, `DISTINCT` or a median, finish with `.sql()`. The result so far is computed in full and made available under the builder's alias:

```python
(
    pond.trickle("orders.order_line").alias("lines")
    .sql("SELECT product_id, median(quantity) AS median_quantity FROM lines GROUP BY product_id")
    .merge("median_quantity", pk="product_id")
)
```

This part is recomputed every run, but the terminal still writes only the rows that changed, so Ponds downstream still receive a small change. An [Ibis](https://ibis-project.org) expression works in place of the SQL string; see [`.sql`](../reference/python/trickle_builder.md#sql).

Aggregations that can be updated incrementally, like sums, counts and means, have their own method. See [Aggregation and Accumulation](aggregation_and_accumulation.md).

## Skipping unchanged runs

A terminal's returned builder reports whether the write changed anything:

```python
out = (
    pond.trickle("orders.order_line")
    .join(pond.trickle("catalog.product"), on="product_id")
    .select("s0.order_id, s0.quantity, s1.unit_price")
    .merge("priced_line", pk="order_id")
)
if not out.was_changed():
    pond.skip()
```

## Sources that aren't Trickles

Any published table can be a source. A plain table written with `write_table` that hasn't changed since the last run costs nothing. One that has changed can't say which rows changed, so the builder recomputes the whole result for that run. If a large, frequently updated input is a plain table, making it a Trickle upstream makes everything downstream of it incremental.

The builder also switches to a full recompute for a run when a source's changes touch more than 30% of its rows, since filtering by changed keys no longer saves work. Change the threshold per source with `pond.trickle("orders.order_line", p=0.5)`.
