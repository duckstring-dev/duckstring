---
title: status, query, get and data
description: Watch Ponds, query and download what they publish, and manage their tables and Objects.
sidebar_label: status, query, get, data
---

# status, query, get and data

Commands for watching a Catchment and for reading, exposing, tracing and deleting what its Ponds publish.

Tables and Objects are named `POND.NAME`, as in the Python API, for example `reports.monthly_summary`. A part that contains dots goes in backticks: ``forecasting.`model.pkl` ``. When a Pond has a table and an Object with the same name, add `--table` or `--object`.

Every command takes `-c` / `--catchment`, and those acting on one Pond take `-m` / `--major` and usually `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `status`

```bash
duckstring status [POND] [-m N | -v VERSION] [--once]
```

Shows every Pond's state (running, queued, idle, failed, blocked or killed), freshness and recurring Refreshes, updating every second until `Ctrl+C`.

| Argument / option | Description |
|---|---|
| `POND` | Only show this Pond and the Ponds upstream of it. |
| `--once` | Print one snapshot and exit. |

## `query`

```bash
duckstring query SQL [--pond POND] [--limit N] [--csv FILE | --json FILE | --parquet FILE] [--path DIR]
duckstring query POND.TABLE
```

Runs read-only SQL and prints the result, or saves it to a file. Given a table instead of SQL, it shows the table's first 10 rows.

SQL runs across the catalog, where each Pond is a schema: `reports` refers to the Pond's served major, and `reports_v2` to a specific one. Users with a read-level key see only exposed tables. With `--pond`, the SQL runs against that Pond's tables, which can then be named without the schema.

```bash
duckstring query reports.monthly_summary
duckstring query "SELECT r.month, r.total_revenue FROM reports_v1.monthly_summary r ORDER BY 1"
duckstring query "SELECT category, SUM(revenue) FROM sale_line GROUP BY 1" --pond sales
duckstring query @queries/top_categories.sql --csv top.csv
```

| Argument / option | Description |
|---|---|
| `SQL` | The query, or `@path/to/file.sql` to read it from a file. |
| `POND.TABLE` | A table, to show its first rows. |
| `--pond`, `-p` | Run the SQL against this Pond's tables, by bare name. |
| `--limit`, `-n` | The most rows to print (default `1000`). Files are written in full. |
| `--csv`, `--json`, `--parquet` | Write the result to a file of this name instead of printing it. |
| `--path` | Directory for the file. Defaults to `./ponds/{pond}/{table}/` for a table, `./ponds/{pond}/` with `--pond`, and the current directory otherwise. |

## `get`

```bash
duckstring get POND.NAME [--out PATH] [--table | --object]
```

Downloads a table's published files, or an Object. A plain table is a single Parquet file, and an append Trickle a directory of files, one per run. A directory Object is unpacked into a folder. `--out` defaults to `./ponds/{pond}/{table}/` for a table and `./{name}` for an Object.

## `data`

### `data ls`

```bash
duckstring data ls [POND] [-m N]
```

Lists each Pond's published tables and Objects: whether each table is exposed to read-level users (and whether that comes from `pond.toml` or an override), and when each last changed. Without `POND`, lists every Pond. `-m` picks a major version; the default is the served major.

### `data expose`

```bash
duckstring data expose POND.TABLE (--on | --off | --default) [-m N]
```

Shows or hides a table from read-level users, overriding the Pond's [`[serve] tables`](../pond_toml.md#serve).

| Option | Description |
|---|---|
| `--on` | Expose the table. |
| `--off` | Hide the table. |
| `--default` | Remove the override and follow `pond.toml`. |

### `data delete`

```bash
duckstring data delete POND.NAME [--yes] [--table | --object]
```

Deletes a table's published data and its state in the Pond's working database, or an Object. Either reappears only if the Pond's code writes it again on a later run. Deleting a table needs the Pond idle. Naming part of a Trickle table, such as its `__changelog`, deletes the whole table.

### `data lineage`

```bash
duckstring data lineage [POND | POND.TABLE] [-m N] [--columns]
```

Shows the tables each Ripple actually read and wrote on its recent runs.

| Argument / option | Description |
|---|---|
| `POND` | Only this Pond. Defaults to every Pond with recorded lineage. |
| `POND.TABLE` | Only the Ripples that read or wrote this table. |
| `--columns` | Also show which source columns each output column is derived from, recorded at deploy. Columns whose source can't be determined exactly are shown as opaque. |

### `data trace`

```bash
duckstring data trace POND.TABLE [--where PREDICATE] [-m N]
```

Finds which run produced some published rows: the newest run among the matching rows, with its version, timings, status, and the window of Source data it read.

| Argument / option | Description |
|---|---|
| `POND.TABLE` | The table. |
| `--where`, `-w` | A SQL condition selecting the rows, such as `"product_id = 7"`. Omit for the whole table. |

```bash
duckstring data trace revenue.revenue_by_product --where "product_id = 7"
```
