---
title: Trickle Builder
description: pond.trickle() and its methods.
---

# Trickle Builder

The builder composes joins, filters, computed columns, aggregations and accumulations over [Trickles](../../concepts/trickles.md) and other tables, and keeps the result up to date incrementally. You start it with `pond.trickle(...)`, chain methods, and finish with a terminal (`.merge()` or `.append()`) that computes and writes the result.

```python
@ripple
def priced_line(pond):
    (
        pond.trickle("orders.order_line")
        .join(pond.trickle("catalog.product"), on="product_id")
        .select("s0.order_id, s0.product_id, s0.quantity, s1.unit_price, "
                "round(s0.quantity * s1.unit_price, 2) AS revenue")
        .merge("priced_line", pk="order_id")
    )
```

Nothing runs until a terminal is called. Every method except the terminals, `count`, `schema`, `to_ibis_schema` and `was_changed` returns the builder, so calls chain.

## How it computes

On each run the builder reads the changes to each source over the run's window and works out the change to its output from them, touching only the join keys that changed. It falls back to recomputing the output in full, and recording only the difference, when:

- a source is being read for the first time, or the Pond has fallen behind that source's retained history;
- a source is a plain table (written with `write_table`) that was republished since the last run;
- a source's changes touch more than its threshold `p` of its rows;
- the output table doesn't exist yet;
- `ivm=False` is passed to the terminal.

Either way, the output table receives only the rows that actually changed.

Expressions must be deterministic. Removals cancel against earlier additions by matching whole rows, so a value like `now()` or `random()` that differs between runs breaks incremental maintenance.

## Sources

### `pond.trickle`

```python
pond.trickle(ref, *, p=0.3) -> TrickleBuilder
```

Starts a builder on a source table.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ref` | `str` | | `"source.table"`. Any published table works: an append or merge Trickle, or a plain table. To use one of this Pond's own tables, write it with a terminal earlier in the same chain (see [Chaining](#chaining)). |
| `p` | `float` | `0.3` | Change-fraction threshold. If this source's changes touch more than `p` of its current rows, the builder recomputes in full for that run, since filtering by key no longer saves work. `1.0` disables the check and skips the row count it needs. |

A plain table that hasn't changed since the last run contributes no changes and costs nothing. One that has changed forces a full recompute, so for large, frequently changing inputs it's worth making the upstream table a Trickle.

### `.alias`

```python
.alias(name) -> TrickleBuilder
```

Names a source so expressions can refer to it as `name.col` instead of its position. Without aliases, sources are named `s0`, `s1`, ... in left-to-right order. Aliases must be unique within a builder. `.sql()` also needs an alias, as the table name its query selects from.

```python
(
    pond.trickle("orders.order_line").alias("o")
    .join(pond.trickle("catalog.product").alias("p"), on="product_id")
    .select("o.order_id, o.quantity, p.unit_price")
    .merge("priced_line", pk="order_id")
)
```

## Joins

### `.join`

```python
.join(other, *, on, how="inner") -> TrickleBuilder
```

Joins another builder on an equality condition. Every join type is maintained incrementally.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `other` | `TrickleBuilder` | | The right-hand side: `pond.trickle(...)`, or itself a chain of joins. |
| `on` | `str`, list or `dict` | | The join key. A column name present on both sides, a list of such names, or a `{left: right}` mapping for differently named columns. A name can be qualified as `alias.col` to pick a side. |
| `how` | `str` | `"inner"` | One of `inner`, `left`, `right`, `full`, `semi`, `anti`. `semi` and `anti` keep only left-side columns. |

Because the right-hand side can itself be a join, bushy and snowflake shapes compose directly:

```python
a_b = pond.trickle("sales.order").join(pond.trickle("sales.customer"), on="customer_id")
c_d = pond.trickle("catalog.product").join(pond.trickle("catalog.supplier"), on="supplier_id")
a_b.join(c_d, on="product_id").merge("order_detail", pk="order_id")
```

The right-hand builder can't carry its own `.filter()`, `.mutate()`, `.select()`, `.aggregate()` or `.sql()`. Apply those to the joined result.

## Shaping the output

`.filter`, `.mutate` and `.select` apply to the composed result in the order they are called, so a filter after a `.mutate()` can use the new column.

### `.filter`

```python
.filter(predicate) -> TrickleBuilder
```

Keeps rows matching a SQL boolean expression.

| Parameter | Type | Description |
|---|---|---|
| `predicate` | `str` | A SQL expression over the columns available at this point: source columns (`s0.col`, or `alias.col`) and any columns added by an earlier `.mutate()`. |

### `.mutate`

```python
.mutate(**columns) -> TrickleBuilder
```

Adds computed columns and keeps all existing ones. Each keyword is a column name and a SQL expression. A name matching an existing column replaces it.

Columns in one call are computed side by side and can't refer to each other; chain another `.mutate()` to build on a new column. A computed column can be used as the output `pk` but not as a join key.

```python
(
    pond.trickle("orders.order_line")
    .mutate(order_date="CAST(s0.ordered_at AS DATE)")
    .filter("order_date >= DATE '2026-01-01'")
    .merge("recent_orders", pk="order_id")
)
```

**Raises** `BuildError` for a column name starting with `_duckstring_`.

### `.select`

```python
.select(projection) -> TrickleBuilder
```

Sets the output columns with a SQL select list, replacing the current set. Items can be computed (`expr AS name`). The list must include the output `pk`.

Without any `.select()`, the output is every column: qualified names become bare names, join keys appear once, and any other name appearing on both sides raises `BuildError`. Use `.select()` to resolve such a clash.

| Parameter | Type | Description |
|---|---|---|
| `projection` | `str` | A SQL select list. |

## Aggregation

### `.aggregate` and `.group_by`

```python
.aggregate(by=None, **metrics) -> TrickleBuilder
.group_by(by).aggregate(**metrics) -> TrickleBuilder
```

Groups the result by `by` and maintains one row per group. Each metric is a keyword argument whose value is an [`agg`](agg.md) spec. The two forms are equivalent.

| Parameter | Type | Description |
|---|---|---|
| `by` | `str` or list of `str` | The grouping columns. They become the output's primary key. |
| `**metrics` | `agg` specs | Output column name to metric. |

Must be finished with `.merge()`, whose `pk` then defaults to `by`. Only one `.aggregate()` per builder; nothing but `.merge()` can follow it.

```python
from duckstring import agg

(
    pond.trickle("priced.priced_line")
    .aggregate(by="product_id",
               total_revenue=agg.sum("revenue"),
               order_count=agg.count(),
               revenue_sd=agg.stddev("revenue"))
    .merge("revenue_by_product")
)
```

**Raises** `BuildError` with no `by`, no metrics, or a metric that isn't an `agg` spec. [`agg.reduce`](agg.md#reduce) needs `.along()` first and can't be combined with other metrics in one call.

## Accumulation

### `.along`

```python
.along(col) -> TrickleBuilder
```

Declares the column that orders rows within each group for `.accumulate()` and `agg.reduce`. For `.accumulate(...).append()`, the column must never decrease from one run to the next, since new rows are assumed to arrive at the end of each group.

### `.accumulate`

```python
.accumulate(by=None, **metrics) -> TrickleBuilder
```

Adds a running value to every row, computed in `.along()` order within each `by` group. Each metric is a keyword argument whose value is an [`acc`](acc.md) spec. The output has one row per input row.

| Parameter | Type | Description |
|---|---|---|
| `by` | `str` or list of `str` | Grouping columns. Omit for a single group. |
| `**metrics` | `acc` specs | Output column name to metric. |

Finish with either terminal:

| Terminal | Input | Cost per run |
|---|---|---|
| `.append()` | Rows only ever added, in increasing `.along()` order. A row older than its group's latest, or a removed row, raises. | Proportional to the new rows. |
| `.merge()` | Any change, including removals and rows arriving out of order. | New rows at the end of a group continue from carried state; a group with an earlier change is recomputed. |

```python
from duckstring import acc

(
    pond.trickle("orders.order_line")
    .along("ordered_at")
    .accumulate(by="store_id",
                running_units=acc.sum("quantity"),
                smoothed_quantity=acc.ema("quantity", alpha=0.2))
    .merge("store_running_units", pk="order_id")
)
```

**Raises** `BuildError` without a prior `.along()`, with no metrics, or with a metric that isn't an `acc` spec.

## Plain SQL

### `.sql`

```python
.sql(query) -> TrickleBuilder
```

Runs any SQL over the result so far, for operations the builder doesn't maintain incrementally: window functions, `DISTINCT`, set operations, holistic aggregates such as a median. The result so far is computed in full and exposed under the builder's `.alias()` name, then `query` runs over it.

The builder that comes back is fully recomputed each run, but its terminal still writes only the rows that changed, so downstream consumers still receive a small change. After `.sql()`, only `.alias()`, `.sql()`, `.schema()`, `.count()` and the terminals are available.

| Parameter | Type | Description |
|---|---|---|
| `query` | `str` or Ibis expression | A SQL string referring to the alias as a table, or an Ibis expression, compiled to DuckDB SQL (requires `ibis-framework`, imported only when used). |

```python
(
    pond.trickle("priced.priced_line").alias("lines")
    .sql("SELECT product_id, median(revenue) AS median_revenue FROM lines GROUP BY product_id")
    .merge("median_revenue", pk="product_id")
)
```

**Raises** `BuildError` without an `.alias()`, or after `.aggregate()`.

### `.schema` and `.to_ibis_schema`

```python
.schema() -> dict[str, str]
.to_ibis_schema() -> dict[str, str]
```

Return the current output's columns and types, as DuckDB types or Ibis type strings, without writing anything. Useful for building an Ibis expression to pass to `.sql()`:

```python
import ibis

b = pond.trickle("priced.priced_line").alias("lines")
t = ibis.table(b.to_ibis_schema(), name="lines")
b.sql(t.group_by("product_id").agg(median_revenue=t.revenue.median())).merge("median_revenue", pk="product_id")
```

`to_ibis_schema` raises for a DuckDB type with no Ibis equivalent.

## Terminals

### `.merge`

```python
.merge(name, *, pk=None, ivm=True, key_filter=True, retain_t=None, retain_n=None) -> TrickleBuilder
```

Computes the result and writes its changes to the merge Trickle `name`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | | Output table. |
| `pk` | `str` or list of `str` | required | The output's primary key, which must be unique. Defaults to the `by` columns after `.aggregate()`. |
| `ivm` | `bool` | `True` | `False` ignores source changes and recomputes the whole output each run, recording the difference. |
| `key_filter` | `bool` | `True` | `False` keeps incremental computation but skips filtering each join to the changed keys. Can help when changes are large. No effect with `ivm=False`. |
| `retain_t`, `retain_n` | | `None` | Change-log retention, as for [`merge_table`](trickle_io.md#merge_table). |

Only change `ivm` or `key_filter` after measuring that the default is slower for a particular build.

**Returns** a builder rooted at the written table (see [Chaining](#chaining)).

**Raises** `BuildError` with no `pk`, or an output that doesn't include the `pk` columns.

### `.append`

```python
.append(name, *, pk=None, fail_on_conflict=True, log_drops=True, ivm=True, key_filter=True,
        retain_t=None, retain_n=None) -> TrickleBuilder
```

Computes the result and adds its new rows to the append Trickle `name`. Suited to transformations whose output rows are only ever added, such as an append-only fact table joined to dimensions.

A removed output row, or an added row whose `pk` is already in the table with different values, is a conflict: it would mean changing history, which an append table can't do. A row identical to one already in the table is skipped.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | | Output table. |
| `pk` | `str` or list of `str` | `None` | The output's primary key. Needed to detect conflicts on changed rows. |
| `fail_on_conflict` | `bool` | `True` | Raise on a conflict, before writing anything. `False` drops conflicting rows and keeps history as it was. |
| `log_drops` | `bool` | `True` | With `fail_on_conflict=False`, record dropped rows in a published `{name}__droplog` table (your columns plus `_duckstring_d` and `_duckstring_f`). |
| `ivm`, `key_filter`, `retain_t`, `retain_n` | | | As for `.merge()`. |

Two rows with the same `pk` and different values within one run always raise.

When the output `pk` is the first source's own key passed through unchanged (`s0.col`), and both `fail_on_conflict` and `log_drops` are `False`, changes to the other sources can't affect the result. The builder then only joins the first source's new rows against the others' current state, which is much cheaper when a joined table changes a lot.

**Returns** a builder rooted at the written table.

**Raises** `BuildError` after `.aggregate()`, and `DeltaError` on a conflict when `fail_on_conflict=True`.

### `.count`

```python
.count() -> int
```

Returns the number of rows the builder represents, computed now. On a bare source or a just-written table, it reads the count from metadata without scanning. On a composed builder, it computes the full current result and counts it. After `.aggregate()`, it returns the number of groups without computing the metrics.

### `.was_changed`

```python
.was_changed() -> bool
```

On a builder returned by a terminal, whether that write changed the output table. Use it to call [`pond.skip()`](pond.md#skip) when nothing changed. After `.aggregate()` or `.accumulate()`, it always returns `True`.

```python
out = pond.trickle("orders.order_line").join(...).merge("priced_line", pk="order_id")
if not out.was_changed():
    pond.skip()
```

## Chaining

The builder a terminal returns is rooted at the table it just wrote, so the chain can continue. Each terminal saves its result as a table that later runs reuse:

```python
(
    pond.trickle("orders.order_line")
    .join(pond.trickle("catalog.product"), on="product_id")
    .select("s0.order_id, s0.product_id, s0.store_id, s0.quantity, s1.unit_price")
    .merge("priced_line", pk="order_id")
    .join(pond.trickle("stores.store"), on="store_id")
    .select("s0.order_id, s0.quantity, s0.unit_price, s1.region")
    .merge("regional_line", pk="order_id")
)
```

When only `stores.store` changes, the second join reuses `priced_line` instead of recomputing the first join. Chaining is also the only way to join on a column that only exists after an earlier join. The steps run one after another; split them into separate Ripples if you want them to run in parallel.

A chained builder can only continue as the left-hand side of a join, not be passed as the right-hand side.

## Errors

| Exception | Import from | Raised when |
|---|---|---|
| `BuildError` | `duckstring.trickle.builder` | The builder is used incorrectly, such as a missing `pk`, an ambiguous join key, or a method used where it isn't allowed. Raised when the terminal is called or when the method is called, whichever detects it first. A subclass of `ValueError`. |
| `DeltaError` | `duckstring.trickle.io` | An `.append()` conflict with `fail_on_conflict=True`. |
