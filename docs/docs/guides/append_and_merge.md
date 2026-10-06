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

### Clustering a merge table

A merge table's older rows are kept in a compacted base, stored as Parquet. Every row group in it records the smallest and largest value of each column, and DuckDB skips the row groups whose range rules out a filter. So a read that filters on a column (a query, a downstream Pond reading only the keys that changed, a lookup) touches little of the base when similar values are stored together, and nearly all of it when they're scattered.

By default the base is ordered by the primary key, so filters on the key skip almost everything. To order it for other columns, declare them on the write:

```python
pond.merge_table("customer", state, pk="customer_id", cluster_by=["region", "signup_day"])
```

With one column, the base is sorted by it. With two or more, the columns are interleaved, because sorting by several columns only clusters the first: within each `region`, `signup_day` would be scattered. Interleaving builds a single ordering key in which rows that are close are close in every column. Duckstring first replaces each value with its rank in its column, in buckets holding about the same number of rows, so a skewed column (most customers in one region) is ordered as finely as an even one. It then walks the grid of buckets along a Hilbert curve, a path that visits every cell and only ever steps to a neighbouring one, and the position along that path is the key. Duckstring calls this a rank-Hilbert order.

On a base of about 100 row groups with two evenly spread columns, the share of row groups a filter on one value touches was:

| Order | Filter on the first column | Filter on the second column |
|---|---|---|
| Primary key | 100% | 100% |
| Sorted by both | 1% | 100% |
| Interleaved | 13% | 14% |

Interleaving three columns touched about 27% for each. Every extra column dilutes the others, so list only the columns that reads filter on, most important first.

To sort by several columns instead, pass `interleave=False`. The interleaved key splits the data into `2**cluster_bits` cells, with the same number of bits for every column (`cluster_bits` is rounded up to a multiple of the column count). By default Duckstring picks `cluster_bits` at each compaction so that a cell is a little under one row group, which is as fine as pruning can use; set it only to fix the precision. A column with NULLs keeps its top bucket for them. On a base of more than ten million rows, the bucket boundaries are approximate quantiles, which cluster just as well and avoid sorting each column first.

Clustering changes how the base is stored, not its contents, so it never affects the version contract. It applies when the base is next rewritten by compaction, and it's declared on every write: leaving `cluster_by` out returns the base to primary-key order at its next compaction. Once a table is clustered by other columns, its primary-key order is gone, so if lookups by key matter too, include the key in `cluster_by`.

A plain table written with `write_table` takes the same options. It's rewritten every run, so it's sorted on every write, and without `cluster_by` it keeps the order its query produced.

## Reading Trickles within the Pond

Later Ripples in the same Pond can query a Trickle by name, like any other table:

```python
@ripple(parents=[ingest])
def expensive(pond):
    pond.write_table("expensive_product", pond.con.sql("SELECT * FROM product WHERE unit_price > 500"))
```

For a merge Trickle, `product` is a view that assembles the current state from the table's stored changes each time it's queried, so it always matches `pond.read_table("product")`. A Ripple that queries it many times can copy it into a temporary table first. The compacted part of the table, without recent changes, is available as `product__base` if you ever need it.

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
