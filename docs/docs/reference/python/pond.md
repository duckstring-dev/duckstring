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

For a Source table, it also registers a view with the table's bare name, so the SQL that follows can refer to it directly (`FROM product`). If one of this Pond's own tables already has that name, the view isn't created, but the returned relation still works. Source reads are pinned to the run: a plain table is read at the version the Source had published when the run started, and a Trickle up to the run's freshness. Every Ripple in a run therefore sees the same Source data, even if the Source publishes again while the run is in progress.

For a Trickle, the result is the clean current state, without the `_duckstring_f` and `_duckstring_d` system columns.

| Parameter | Type | Description |
|---|---|---|
| `ref` | `str` | `"table"` or `"source.table"`. |

**Raises** `MissingSourceAsset` (a subclass of `FileNotFoundError`) when a Source table hasn't been published. Duckstring treats this as waiting rather than failing: the Pond is parked until the Source publishes again, with no retry spent and no alert sent.

**Raises** `duckstring.core.RippleOrderError` when reading one of the Pond's own tables that another Ripple writes, if that Ripple isn't among this Ripple's parents (directly or further up). The read could otherwise see the previous run's data. `count_table` checks the same.

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
pond.write_table(name, relation, *, if_changed=False, cluster_by=None, interleave=True, cluster_bits=None) -> bool
```

Replaces the table `name` in the Pond's database with the contents of `relation`, in one transaction. A write that collides with another Ripple's write is retried rather than failing.

Every table in the Pond's database is published when the whole Pond Run succeeds. If any Ripple fails, nothing from the run is published.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Table name. Tables whose names start with `_duckstring_` are internal and never published, and columns with that prefix are rejected at publish. |
| `relation` | `duckdb.DuckDBPyRelation` | The new contents. |
| `if_changed` | `bool` | Write only if [`detect_change`](#detect_change) finds that `relation` differs from the published table. When it doesn't, nothing is written and the published table is kept as it is. |
| `cluster_by`, `interleave`, `cluster_bits` | | Order the table as it's written, as for [`merge_table`](trickle_io.md#merge_table), so reads filtering on those columns skip most of it. The table is sorted on every write. Without `cluster_by`, it keeps the order `relation` produced. |

**Returns** `True` if the table was written. Without `if_changed` it always is.

**Raises** `DeltaError` for an invalid clustering, such as a column `relation` doesn't have.

To keep history for downstream incremental reads, use [`append_table`](trickle_io.md#append_table) or [`merge_table`](trickle_io.md#merge_table) instead.

## Objects

Objects are named, non-tabular outputs: a trained model, a serialised vectoriser, a rendered report. Each is a single file or a directory, published and replaced as one unit.

### `write_object`

```python
pond.write_object(name, src, *, if_changed=False) -> bool
```

Stages an Object to be published with the run's tables. A later Ripple failure leaves the previously published Object in place.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Object name: letters, digits and underscores, starting with a letter or underscore, optionally with single dots between parts (`model.pkl`). Read a dotted name back with backticks. |
| `src` | path, `bytes` or binary file-like | A file path, a directory path (published as one Object), raw bytes, or an open binary file. |
| `if_changed` | `bool` | Publish only if the content differs from the published Object, compared by a SHA-256 of the payload. When it doesn't, the published Object is kept, with the freshness of the run that last changed it. |

**Returns** `True` if the Object was staged for publishing. Without `if_changed` it always is.

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
    model = pickle.loads(pond.read_object("`model.pkl`"))
    ...
```

## Change detection

Duckstring skips a Pond Run when none of its Sources changed since the last run. These methods let a Ripple take part in that decision, and find out which tables and Objects changed so it can skip just the work that depends on the rest.

Every published table and Object has its own change freshness: the freshness of the run that last changed it. A Trickle table changes when a run writes rows to it. An Object changes when it's written. A plain table counts as changed every time it's written, unless it's written with `if_changed=True` (or checked with [`detect_change`](#detect_change) and then not written), in which case a run that finds it unchanged keeps the published version, and its change freshness stays where it was. Downstream, that table then reads as unchanged, so [`read_delta`](trickle_io.md#read_delta) returns an empty delta for it and the [Trickle builder](trickle_builder.md) doesn't recompute in full because of it.

### `changed`

```python
pond.changed(*refs) -> bool
```

Whether any of `refs` changed, as seen by this run. With no arguments, whether this run has changed any of the Pond's own tables or Objects so far.

| Reference | Changed when |
|---|---|
| A Source's plain table | The version this run reads was published after the previous run. |
| A Source's append or merge Trickle | Rows were added, changed or removed in the window `(previous_f, f]`, or the window can't be read as changes (the Pond's first run, or `previous_f` older than the Source's retained history). |
| A Source's Object | It was written after the previous run. |
| One of this Pond's own tables or Objects | This run wrote it and didn't keep the published copy. A Trickle write counts only if it changed the table. |

| Parameter | Type | Description |
|---|---|---|
| `refs` | `str` | Table or Object references, `"name"` or `"source.name"`. |

The own forms only see writes made through the handle (`write_table`, `write_object`, `append_table`, `merge_table`, `apply_zset` and the builder's terminals) and checks made with `detect_change`. A table changed with SQL on `pond.con` doesn't count.

A Source is compared with the previous run's freshness. When a Pond reads several Sources that share an upstream, it can occasionally report `True` for a change the previous run already read. That costs a redundant recompute, never a missed change.

**Raises** `RippleOrderError` for one of the Pond's own tables written by a Ripple that isn't this Ripple or one of its ancestors, as for `read_table`, and `MissingSourceAsset` for a Source table or Object that hasn't been published.

To mark the whole run unchanged when none of the Pond's own output changed, ask from a Ripple that runs after the others:

```python
@ripple(parents=[load_rates, load_products])
def done(pond):
    if not pond.changed():
        pond.skip()
```

To skip one Ripple's work when an input it reads didn't change:

```python
@ripple
def price_tiers(pond):
    if not pond.changed("products.product"):
        return                              # price_tiers stays as it was
    ...
```

### `detect_change`

```python
pond.detect_change(name, value, *, exact=False) -> bool
```

Whether `value` differs from what is published as this Pond's own table or Object `name`. Nothing is written.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | One of this Pond's own tables or Objects. |
| `value` | relation, path, `bytes` or binary file-like | A `duckdb.DuckDBPyRelation` is compared as a table. Anything else is compared as an Object; a file-like is read, then rewound when it can be. |
| `exact` | `bool` | Tables only. Compare the rows themselves (`EXCEPT ALL` in both directions) against the Pond's copy of the table, instead of fingerprints. Slower, but it can't be fooled by a hash collision. |

A table is compared by fingerprint: a hash over every row, independent of row order and counting duplicate rows, combined with the column names and types. It costs one scan of `value`. An Object is compared by a SHA-256 of its payload, with a directory hashed file by file.

**Returns** `True` whenever it can't show the two are the same: nothing published yet, a table last written without a fingerprint, a fingerprint taken by a different DuckDB version (an upgrade costs one republish per table), or a Pond database that no longer holds the published table (a failed run left newer data there, which later Ripples would read).

If it returns `False` and the run doesn't then write `name`, the published copy is kept. Writing anyway is always allowed and counts as a change, so you can confirm a fingerprint match your own way first. `write_table(..., if_changed=True)` and `write_object(..., if_changed=True)` are shorthands for checking and then writing. A `write_table` of the same relation object right after a check records its fingerprint, so the next run can compare against it.

### `sources_changed`

```python
pond.sources_changed() -> bool
```

Whether any Source's output changed since this Pond last ran. Always `True` in a local run. Mainly useful in a Ripple declared with `always_run=True`, which runs regardless and can use this to skip its data work.

### `skip`

```python
pond.skip() -> None
```

Marks this Pond Run as producing no change. Downstream Ponds then treat this Pond's output as unchanged, and a Pond whose Sources are all unchanged passes without running. Freshness still advances. Calling it from any Ripple marks the whole run, so call it once you know nothing the Pond publishes changed, usually from a Ripple that runs last. Has no effect in a local run.

`changed()` with no arguments is the usual signal for calling it. The Trickle write methods also return whether they changed anything:

```python
@ripple
def by_product(pond):
    pond.read_table("priced.priced_line")
    totals = pond.con.sql("SELECT product_id, SUM(revenue) AS total_revenue FROM priced_line GROUP BY product_id")
    changed = pond.merge_table("revenue_by_product", totals, pk="product_id")
    if not changed:
        pond.skip()
```
