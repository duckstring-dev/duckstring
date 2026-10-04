---
title: Puddle Handle
description: The object passed to every @puddle function.
---

# Puddle Handle

Every [`@puddle`](decorators.md#puddle) function receives a `Puddle` object. It writes sample Source data into `puddles/ponds/{source}/data/`, where `duckstring pond run` reads it as if it were the Source's published output.

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
| `con` | `duckdb.DuckDBPyConnection` | A scratch in-memory DuckDB connection. |
| `path` | `pathlib.Path` | The directory the Puddle writes into, `puddles/ponds/{source}/data/`. Created when first accessed. Anything written here directly is visible to the local run. |
| `source` | `str` | The Source name from the decorator target. |
| `table` | `str` or `None` | The table name from the decorator target, or `None` for a whole-Source Puddle. |

## Methods

### `write_table`

```python
p.write_table(relation) -> pathlib.Path
p.write_table(name, relation) -> pathlib.Path
```

Writes a relation to `{path}/{name}.parquet` and returns the file path. The one-argument form uses the table named in the decorator target; a whole-Source Puddle must name each table.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Table name. |
| `relation` | DuckDB relation or DataFrame | The data. A pandas DataFrame is converted through `con`. |

**Raises** `ValueError` for the one-argument form on a whole-Source Puddle.

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
