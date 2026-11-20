---
title: Puddle Handle
description: The object passed to every @puddle function.
---

# Puddle Handle

Every [`@puddle`](decorators.md#puddle) and [`@puddle.step`](decorators.md#puddlestep) function receives a `Puddle` object. It writes stand-in Source data into `puddles/ponds/{source}/data/`, published as the Source would publish it, where `duckstring pond run` reads it as if it were the Source's own output.

```python
from duckstring import puddle

@puddle("transactions.transaction")
def transactions(p):
    p.write_table(p.con.sql("""
        SELECT i AS id, DATE '2026-01-01' + i AS created_at, 1 + i % 10 AS product_id,
               1 + i % 5 AS quantity, 1 + i % 3 AS store_id
        FROM range(100) t(i)
    """))
```

## Attributes

| Attribute | Type | Description |
|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | An in-memory DuckDB connection for building the Puddle's data. Relations made on it can be written or returned. Make scratch tables `TEMP`: other tables left on it aren't kept. |
| `path` | `pathlib.Path` | The directory the Puddle writes into, `puddles/ponds/{source}/data/`. Created when first accessed. Anything written here directly is visible to the local run. |
| `source` | `str` | The Source name from the decorator target. |
| `table` | `str` or `None` | The table name from the decorator target, or `None` for a whole-Source Puddle. |
| `f` | `datetime` | The freshness the Puddle's data is stamped with: the hydration time, or for a Puddle that reads its Source, the Source's freshness then; in a step, the window it writes. |
| `previous_f` | `datetime` | In a step, the freshness of the Puddle's previous window. Otherwise `datetime.min` (UTC). |
| `step` | `int` | In a step, its number since the baseline (1 for the first). Otherwise 0. |

## Methods

### `read_source`

```python
p.read_source(table=None) -> DuckDBPyRelation
```

The Source's published table on the Catchment the Puddle hydrates from, as a relation on `con`. `table` defaults to the target's. The Source's major line is the one its `[sources]` pin selects.

The relation stands in for the table: it has the table's columns and no rows. What the definition writes or returns from it is recorded as a query, which runs on the Catchment through its [query API](../cli/data.md#query) as of each point the Puddle is read at, so only the rows it keeps come back. Build on it with DuckDB relations (filters, projections, joins between the Source's tables, aggregates, samples by key hash). A definition that uses it can be read live (`pond hydrate --live`) and replayed through the Source's history (`--as-at`), so it must give the same rows for the same data.

**Raises** `ValueError` when the Catchment can't be asked, or doesn't publish the table. Hydrating fails the definition when what it writes from `read_source` isn't a query over the Source alone (rows fetched into Python, or other tables mixed in).

### `write_table`

```python
p.write_table(relation)
p.write_table(name, relation)
```

Writes a table of the Puddle. The one-argument form uses the table named in the decorator target; a whole-Source Puddle must name each table. A query over `read_source` is read as the kind the Source publishes the table as (append, merge or plain), so incremental reads of the Puddle behave as reads of the Source would. Otherwise it is a plain table.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Table name. |
| `relation` | DuckDB relation or DataFrame | The data. A pandas DataFrame is converted through `con`. |

**Raises** `ValueError` for the one-argument form on a whole-Source Puddle.

### `append_table` and `merge_table`

```python
p.append_table(relation, *, pk=None, on_conflict="skip_check", log_drops=True)
p.append_table(name, relation, ...)
p.merge_table(relation, *, pk)
p.merge_table(name, relation, *, pk)
```

Write an append or merge table of the Puddle, as [`pond.append_table`](trickle_io.md#append_table) and [`pond.merge_table`](trickle_io.md#merge_table) do, stamped at `p.f`. Use them for a Puddle of a Source that publishes a Trickle, so a Pond reading its deltas sees the changes each step makes. `merge_table` takes the complete current state, and the difference from the previous state is the change.

### `read_table`

```python
p.read_table(name=None) -> DuckDBPyRelation
```

The Puddle's own table as written so far, by the baseline and any earlier steps, as a relation on `con`. `name` defaults to the target's table.

### `write_path`

```python
p.write_path(src) -> None
```

Copies existing Parquet or CSV files into the Puddle. For a single-table Puddle, everything matched becomes that table. For a whole-Source Puddle, each file becomes a table named after the file.

| Parameter | Type | Description |
|---|---|---|
| `src` | path or `str` | A file path or glob, such as `"~/samples/*.parquet"`. |

**Raises** `FileNotFoundError` when a glob matches nothing.

### `catchment`

```python
p.catchment(name=None) -> Catchment
```

Returns a [Catchment client](catchment.md) whose default Pond is this Puddle's Source and default table is its target table, sharing `con`. Use it to sample real data from a running Catchment:

```python
@puddle("products.product")
def product(p):
    p.write_table(p.catchment().query("SELECT * FROM product USING SAMPLE 10%"))
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | the default Catchment | A registered Catchment name, or a URL. |

### Objects

| Method | Description |
|---|---|
| `write_object(name, src)` | Seeds an Object for the Source from a path (file or directory), `bytes`, or a binary file-like, so a Ripple reading `"{source}.{name}"` finds it. |
| `read_object(name)` | Returns the bytes of a seeded single-file Object. |
| `object_path(name)` | Returns a local path to a seeded Object, file or directory. |
