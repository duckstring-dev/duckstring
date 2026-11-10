---
title: Databricks Inputs
description: Reading Unity Catalog tables and files in a Volume into a Pond.
---

# Databricks Inputs

```python
from duckstring import databricks
```

Two functions read Databricks data in Ripple code: [`table`](#table) for a Unity Catalog table, and [`files`](#files) for the Delta or Parquet files a Databricks job wrote, usually to a Volume. Each returns an [`Input`](#input): the data as a DuckDB relation, whether it changed since the Ripple's previous run, and the rows added since then.

```python
from duckstring import databricks, ripple


@ripple
def ingest(pond):
    src = databricks.table(pond, "main.sales.customer")
    if not src.changed:
        pond.skip()
        return
    pond.merge_table("customer", src.read(), pk="customer_id")
```

[Reading from Databricks](../../guides/ingesting_data.md#reading-from-databricks) covers when to use each.

## Connecting

`table`, and `files` for a Volume that isn't mounted, reach the workspace named by `DATABRICKS_HOST`. They sign in with the first of these that is set:

1. `DATABRICKS_TOKEN`, a personal access token.
2. `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET`, a Databricks service principal's OAuth credentials. A Databricks App is given these for its own service principal.
3. On Azure Databricks, a Microsoft Entra token from the Duck's Azure identity. This needs the `azure` extra.

They're read from the Duck's environment, like any credentials a Ripple uses. A `/Volumes/...` path reached through the Files API needs the `duckstring[databricks]` extra. Delta tables are read with DuckDB's `delta` extension, which DuckDB downloads on first use.

## `table`

```python
databricks.table(pond, name, *, warehouse=None, env=None) -> Input
```

A Unity Catalog table or view, as an `Input`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pond` | Pond handle | | The Ripple's `pond`. |
| `name` | `str` | | The full name, `catalog.schema.table`. |
| `warehouse` | `str` | `DUCKSTRING_DATABRICKS_WAREHOUSE`, else `DATABRICKS_WAREHOUSE_ID` | The id of the SQL warehouse to read through when the table can't be read from its storage. |
| `env` | mapping | the process environment | Where the host, credentials and default warehouse are read from. |

The version is the table's latest Delta version, from Unity Catalog's table information. Getting it runs no warehouse. A view, or a table that isn't Delta, has no version.

The data is read one of two ways, chosen at the first `read()` or `added()`:

- From the table's storage, with read credentials Unity Catalog vends for it (`via` is `"storage"`). No warehouse runs. This needs the metastore's External data access turned on and `EXTERNAL USE SCHEMA` on the table's schema, and works for tables on Azure and AWS storage.
- Otherwise through the warehouse (`via` is `"warehouse"`), as `SELECT * ... VERSION AS OF` the version, with the result downloaded as Parquet into the [input cache](#the-input-cache). A view, a table that isn't Delta, and a table whose commits Unity Catalog manages (a `delta.feature.catalog*` property) are always read this way.

**Raises** `duckstring.databricks_rest.DatabricksError` when the table can't be found, and at the first read when it can't be read from storage and no warehouse is set. The message says why storage was refused.

## `files`

```python
databricks.files(pond, path, *, format=None) -> Input
```

The files at `path`, as an `Input`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pond` | Pond handle | | The Ripple's `pond`. |
| `path` | `str` | | A `/Volumes/...` path, a local path, or an object-store URI. |
| `format` | `str` | detected | `"delta"` or `"parquet"`. Without it, a directory with a `_delta_log` is read as Delta and anything else as Parquet. |

A Delta table is read at its newest version, with deletion vectors applied.

For Parquet, every `.parquet` file under `path` is read, with columns matched by name. Directories named `key=value` become columns, and directories starting with `_` or `.` are skipped. A file written by a Spark job counts once the job's `_committed_<id>` marker lists it, and stops counting when a later job's marker removes it, so output a job is still writing is left for the next run. Any other file counts unless it's empty, since on a Volume a file is empty while it's uploading. The version is a digest of the committed files and their sizes.

Files are read where they are when DuckDB can reach them: a local path, a mounted Volume or an object store (`via` is `"files"`). A Volume that isn't mounted, as in a Databricks App, is reached through the Files API, and the files are copied into the [input cache](#the-input-cache) first (`via` is `"cache"`).

**Raises** `FileNotFoundError` when `path` doesn't exist or its Delta log has no commits, and `ValueError` for an unknown `format`. A directory with no committed Parquet files yet raises `FileNotFoundError` at the first read.

## `Input`

| Attribute | Type | Description |
|---|---|---|
| `ref` | `str` | What was read: the table's full name, or the path. |
| `version` | `str` or `None` | The input's version now: a Delta version, or a digest of the committed files. `None` for a view, which has no version. |
| `changed` | `bool` | Whether the version differs from the one the Ripple's previous completed run read. Always `True` on a first run, after a Rebuild, and for an input without a version. |
| `via` | `str` or `None` | How the data was reached, once read: `"storage"`, `"warehouse"`, `"files"` or `"cache"`. |

Calling `table` or `files` records the input's version for the run. The record is kept in the Pond's database and published with the run, so `changed` and `added()` still compare against the previous run after a Duck rebuilds its database. A run that fails doesn't count as having read it. A [Rebuild](../cli/control.md#rebuild) starts over: everything reads as changed and added.

### `read`

```python
src.read() -> duckdb.DuckDBPyRelation
```

The input's current data, as a relation on `pond.con`.

### `added`

```python
src.added() -> duckdb.DuckDBPyRelation
```

The rows added since the Ripple's previous completed run: everything on a first run, after a Rebuild or for an input without a version, and no rows when the input is unchanged. On the warehouse an unchanged `added()` still runs the full query, so check `changed` first.

What counts as added:

- Parquet files: the rows of the files that are new since the previous run.
- Delta, when every commit since the previous version only added files, without deletion vectors: the rows of the added files.
- Delta otherwise, such as after an `OPTIMIZE`: the rows of the current version that aren't in the previous one.

**Raises** [`NotAppendOnly`](#notappendonly) when rows were changed or removed: a previous Parquet file is gone, or a row of the previous Delta version isn't in the current one.

## `NotAppendOnly`

```python
class databricks.NotAppendOnly(ValueError)
```

Raised by `added()` when the input isn't append-only. Read it whole with `read()` and write it with [`merge_table`](trickle_io.md#merge_table) instead.

## The input cache

Files fetched through the Files API, and results downloaded from a warehouse, are kept under `DUCKSTRING_INPUT_CACHE` (default the system's temporary directory), in `duckstring-inputs/`, one directory per Pond and input. Only files the cache doesn't hold yet are fetched, and files the current read doesn't need are removed, so it holds about one copy of each input. A table's warehouse results are removed the next time `table` is called for it.
