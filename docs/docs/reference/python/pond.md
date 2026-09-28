---
title: Pond Handle
description: The object passed to every Ripple.
---

# Pond Handle

Every Ripple receives a `Pond` object as its only argument. It holds a DuckDB connection to the Pond's own database and the methods for reading and writing data. You never construct one yourself.

```python
@ripple
def daily_sales(pond):
    pond.read_table("transactions.transaction")
    pond.write_table("daily_sales", pond.con.sql("SELECT ... FROM \"transaction\" ..."))
```

Table and Object references take two forms throughout:

- `"table"`: one of this Pond's own tables.
- `"source.table"`: a table published by the Source Pond `source`, which must be listed in `[sources]`.

A reference is split at its first dot. For a name that itself contains a dot, put it in backticks: ``"`daily.v2`"`` is this Pond's own `daily.v2`, and ``"sales.`daily.v2`"`` is the Source's. Dotted names work but are best avoided. A reference to a Source the Pond doesn't declare raises `ValueError`, suggesting backticks if you meant a dotted name of your own.

The Incremental methods (`append_table`, `merge_table`, `apply_zset`, `read_delta`, `trickle`) are documented on [Trickle I/O](trickle_io.md) and [Trickle Builder](trickle_builder.md).

## Attributes

| Attribute | Type | Description |
|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | Connection to the Pond's database (its registry), shared by all its Ripples. Tables written here are published when the Pond Run succeeds. |
| `name` | `str` | The Pond's name. |
| `version` | `str` | The deployed version, such as `"1.2.0"`. |
| `f` | `datetime` | The freshness of this Pond Run, timezone-aware UTC. It stays the same across retries and crash recovery of the same run, so it is safe to use as a watermark or provenance stamp. In a local run it is the run's start time. |
| `previous_f` | `datetime` | The freshness of the previous successful run. On the first run it is `datetime.min` in UTC, so the window `(previous_f, f]` covers everything. |

## Reading tables

### `read_table`

```python
pond.read_table(ref) -> duckdb.DuckDBPyRelation
```

Returns a relation over a table's current contents.

For a Source table, it also registers a view with the table's bare name, so the SQL that follows can refer to it directly (`FROM product`). If one of this Pond's own tables already has that name, the view isn't created, but the returned relation still works. Source reads are pinned to this run's freshness where the data plane keeps history (the default Iceberg plane does), so every Ripple in a run sees the same Source snapshot even if the Source publishes again mid-run.

For a Trickle, the result is the clean current state, without the `_duckstring_f` and `_duckstring_d` system columns.

| Parameter | Type | Description |
|---|---|---|
| `ref` | `str` | `"table"` or `"source.table"`. |

**Raises** `MissingSourceAsset` (a subclass of `FileNotFoundError`) when a Source table hasn't been published. Duckstring treats this as waiting rather than failing: the Pond is parked until the Source publishes again, with no retry spent and no alert sent.

:::note
Refer to Source tables by their registered view name in SQL. Referring to a Python variable that holds a relation (`FROM rel`) relies on DuckDB scanning Python frames, which is unreliable under the Duck's threaded executor.
:::

### `count_table`

```python
pond.count_table(ref) -> int
```

Returns the current number of rows in a table without scanning it where possible. For a Trickle, the count comes from file metadata and the net weight of the change log.

| Parameter | Type | Description |
|---|---|---|
| `ref` | `str` | `"table"` or `"source.table"`. |

## Writing tables

### `write_table`

```python
pond.write_table(name, relation) -> None
```

Replaces the table `name` in the Pond's database with the contents of `relation`, in one transaction. A write that collides with another Ripple's write is retried rather than failing.

Every table in the Pond's database is published when the whole Pond Run succeeds. If any Ripple fails, nothing from the run is published.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Table name. Tables whose names start with `_duckstring_` are internal and never published, and columns with that prefix are rejected at publish. |
| `relation` | `duckdb.DuckDBPyRelation` | The new contents. |

To keep history for downstream incremental reads, use [`append_table`](trickle_io.md#append_table) or [`merge_table`](trickle_io.md#merge_table) instead.

## Objects

Objects are named, non-tabular outputs: a trained model, a serialised vectoriser, a rendered report. Each is a single file or a directory, published and replaced as one unit.

### `write_object`

```python
pond.write_object(name, src) -> None
```

Stages an Object to be published with the run's tables. A later Ripple failure leaves the previously published Object in place.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Object name: letters, digits and underscores, starting with a letter or underscore, optionally with single dots between parts (`model.pkl`). Read a dotted name back with backticks. |
| `src` | path, `bytes` or binary file-like | A file path, a directory path (published as one Object), raw bytes, or an open binary file. |

**Raises** `RuntimeError` outside a Pond Run.

### `read_object`

```python
pond.read_object(ref) -> bytes
```

Returns the bytes of a single-file Object. Reading one of this Pond's own Objects returns the version staged in this run if there is one, otherwise the published one.

| Parameter | Type | Description |
|---|---|---|
| `ref` | `str` | `"name"` or `"source.name"`. |

**Raises** `duckstring.objects.ObjectError` for a directory Object; use `object_path` instead.

### `object_path`

```python
pond.object_path(ref) -> pathlib.Path
```

Returns a local path to an Object, file or directory. An Object held in object storage is downloaded once per run to a scratch directory. Treat the path as read-only.

| Parameter | Type | Description |
|---|---|---|
| `ref` | `str` | `"name"` or `"source.name"`. |

```python
import pickle

@ripple
def train(pond):
    model = fit(pond.read_table("sales.sale_line").df())
    pond.write_object("model.pkl", pickle.dumps(model))

@ripple(parents=[train])
def score(pond):
    model = pickle.loads(pond.read_object("model.pkl"))
    ...
```

## Skipping unchanged work

Duckstring skips a Pond Run when none of its Sources changed since the last run. These methods let a Ripple take part in that decision.

### `sources_changed`

```python
pond.sources_changed() -> bool
```

Whether any Source's output changed since this Pond last ran. Always `True` in a local run. Mainly useful in a Ripple declared with `always_run=True`, which runs regardless and can use this to skip its data work.

### `skip`

```python
pond.skip() -> None
```

Marks this Pond Run as producing no change. Downstream Ponds then treat this Pond's output as unchanged and can skip their own runs. Freshness still advances. Has no effect in a local run.

The Trickle write methods return whether they changed anything, which is the usual signal for calling `skip()`:

```python
@ripple
def by_product(pond):
    pond.read_table("priced.priced_line")
    totals = pond.con.sql("SELECT product_id, SUM(revenue) AS total_revenue FROM priced_line GROUP BY product_id")
    changed = pond.merge_table("revenue_by_product", totals, pk="product_id")
    if not changed:
        pond.skip()
```
