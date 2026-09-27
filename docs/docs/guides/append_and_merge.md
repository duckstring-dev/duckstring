---
title: Append and Merge Tables
description: Write tables that downstream Ponds can read incrementally.
---

# Append and Merge Tables

A table written with `write_table` is replaced in full each run, and a Pond reading it downstream has to reprocess all of it. Writing it as a [Trickle](../concepts/trickles.md) instead keeps a record of what changed, so downstream Ponds can process just the changes. This guide covers the two kinds of Trickle and how to read their changes directly. For joins and aggregations over Trickles, see [Joins with the Builder](joins_with_the_builder.md).

## Choosing a table type

| Your table | Write it with |
|---|---|
| Rows are only ever added: events, logs, transactions, facts that never change | `append_table` |
| Rows are inserted, updated and deleted: dimensions, current state, anything with a primary key whose values change | `merge_table` |
| Small, or always consumed in full anyway | `write_table` |

The choice is made per table, when the Ripple writes it. Keep to one method for a given table.

## Append tables

The demo `orders` Pond appends a batch of order lines each run:

```python
from duckstring import ripple


@ripple
def ingest(pond):
    batch = pond.con.sql("SELECT ... AS order_id, ... AS product_id, ... AS quantity FROM ...")
    pond.append_table("order_line", batch, pk="order_id", fail_on_conflict=False)
```

Each row is stamped with the run's freshness. Downstream, `pond.read_delta("orders.order_line")` returns just the rows added since that Pond last ran.

`pk` is optional. With it, `append_table` checks by default that the key is unique within the batch and not already in the table, and raises before writing if it isn't. Here the order IDs are unique by construction, so `fail_on_conflict=False` skips the check, which saves reading the table's history on every run.

A retried run, or one replayed after a crash, has the same freshness, and its rows replace the ones it wrote before rather than duplicating them.

## Merge tables

The demo `catalog` Pond writes the complete product catalogue every run, and lets Duckstring work out what changed:

```python
@ripple
def ingest(pond):
    state = pond.con.sql("SELECT product_id, name, category, unit_price FROM ...")
    pond.merge_table("product", state, pk="product_id")
```

`merge_table` compares the new state with the previous one and records the difference. A row with a new key is an insert, a row whose values changed is an update, and a key missing from the new state is a delete. Always pass the whole table: a partial batch would record every missing row as deleted.

`pk` is required, and must be unique in the state you pass. Duckstring doesn't check this, and duplicate keys leave the table's current state undefined, so deduplicate first if the source might repeat keys.

When only a few of 100,000 products change price, only those rows reach the change log, and only the order lines for those products are recomputed downstream.

### Merging a recomputed result

`merge_table` is also useful when a Ripple can only recompute its output in full. The demo `revenue` Pond re-aggregates every order line each run, but writes the totals with a merge:

```python
@ripple
def by_product(pond):
    pond.read_table("priced.priced_line")
    totals = pond.con.sql("""
        SELECT product_id, ROUND(SUM(revenue), 2) AS total_revenue, SUM(quantity) AS units_sold
        FROM priced_line GROUP BY product_id
    """)
    pond.merge_table("revenue_by_product", totals, pk="product_id")
```

The computation isn't incremental, but its output is: anything downstream receives only the products whose totals moved. For an aggregation maintained incrementally from the start, see [Aggregation and Accumulation](aggregation_and_accumulation.md).

## Reading Trickles within the Pond

A later Ripple in the same Pond must read a Trickle through `pond.read_table`, which returns its current state. Don't query the table's name directly in SQL: a merge Trickle's current state is assembled from its change log when read, so the table of that name may be missing or out of date. To use it in SQL, register the current state as a view under another name:

```python
@ripple(parents=[ingest])
def expensive(pond):
    pond.read_table("product").create_view("product_now", replace=True)
    pond.write_table("expensive_product", pond.con.sql("SELECT * FROM product_now WHERE unit_price > 500"))
```

Downstream Ponds don't have this problem: `pond.read_table("catalog.product")` always returns the current state.

## Reading changes directly

`pond.read_delta` returns a Source table's changes since this Pond last ran, as a [`Delta`](../reference/python/trickle_io.md#delta). The builder is usually the better way to transform changes, but reading them directly suits custom logic and side effects, such as notifying another system about new rows:

```python
@ripple
def announce_large_orders(pond):
    delta = pond.read_delta("orders.order_line")
    if delta.is_full:
        return                          # first run, or fell behind: don't re-announce history
    for order_id, quantity in delta.upserts.filter("quantity >= 50").project("order_id, quantity").fetchall():
        post_to_sales_channel(order_id, quantity)
```

`delta.zset` holds the changed rows with a `_duckstring_d` weight: `+1` for a row added and `-1` for a row removed, with an update appearing as both. `delta.upserts` is the rows present after the change, and `delta.deletes` the keys removed.

Always handle `is_full`. It's `True` on the Pond's first run, after a [refresh](../reference/cli/control.md#refresh), when the Pond has fallen behind the Source's retained history, and whenever the Source is a plain table that was republished. In those cases the delta is the whole current table, and treating it as a batch of new rows would double-count.

## Skipping downstream work

The write methods return whether they changed anything. When nothing did, call `pond.skip()` so downstream Ponds can skip their runs too:

```python
@ripple
def ingest(pond):
    changed = pond.merge_table("product", state, pk="product_id")
    if not changed:
        pond.skip()
```

If a Pond writes several tables, only skip when none of them changed.

## Retention

A Trickle's change history grows with every run. Duckstring folds older merge changes into the table automatically, but append history and recent merge changes are kept until you bound them:

```python
from datetime import timedelta

pond.append_table("event", batch, retain_t=timedelta(days=30))     # keep 30 days of history
pond.merge_table("product", state, pk="product_id", retain_n=100)  # keep the last 100 runs of changes
```

Retention limits how far behind a downstream Pond can fall and still read just the changes. One that falls further behind reads the whole table instead, so retention affects cost, never correctness. For an append table, trimmed history is gone from the table itself too, so only bound it when old rows are no longer needed.
