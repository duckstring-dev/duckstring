---
title: Data and status
description: status, query, get, objects, lineage, trace and deletes.
sidebar_label: Data and status
---

# Data and status

Top-level commands for watching a Catchment and reading, tracing and deleting its data.

Every command takes `-c` / `--catchment`, and those acting on one Pond take `-m` / `--major` and usually `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `status`

```bash
duckstring status [POND] [-m N | -v VERSION] [--once]
```

Shows every Pond's state (running, queued, idle, failed, blocked or killed), freshness and standing trigger, refreshing every second until `Ctrl+C`.

| Argument / option | Description |
|---|---|
| `POND` | Only show this Pond and the Ponds upstream of it. |
| `--once` | Print one snapshot and exit. |

## Reading data

### `query`

```bash
duckstring query POND [TABLE] [--sql SQL] [--csv FILE | --json FILE | --parquet FILE] [--path DIR]
```

Runs read-only SQL against one Pond's published tables and prints the result. With only `TABLE`, runs `SELECT * FROM {pond}.{table} LIMIT 10`.

| Argument / option | Description |
|---|---|
| `POND` | The Pond. |
| `TABLE` | A table, for the default query. |
| `--sql` | The query, or `@path/to/file.sql` to read it from a file. Tables can be referred to by bare name or as `{pond}.{table}`. |
| `--csv`, `--json`, `--parquet` | Write the result to a file of this name instead of printing it. |
| `--path` | Directory for the output file. Defaults to `./ponds/{pond}/{table}/`, or `./ponds/{pond}/` without a table. |

To query across several Ponds, use [`serve query`](serve.md#query).

### `get`

```bash
duckstring get POND TABLE [--path DIR]
```

Downloads a table's published files. A plain table is a single Parquet file; an append Trickle is a directory of files, one per run. `--path` defaults to `./ponds/{pond}/{table}/`.

### `objects`

```bash
duckstring objects POND
```

Lists the Pond's published Objects: name, whether each is a file or directory, size, and the freshness of the run that wrote it.

### `get-object`

```bash
duckstring get-object POND NAME [--out PATH]
```

Downloads an Object. A directory Object is unpacked into a folder. `--out` defaults to `./{name}`.

## Lineage

### `lineage`

```bash
duckstring lineage [POND] [--table TABLE] [-m N] [--columns]
```

Shows the tables each Ripple actually read and wrote on its recent runs.

| Argument / option | Description |
|---|---|
| `POND` | Only this Pond. Defaults to every Pond with recorded lineage. |
| `--table`, `-t` | Only Ripples that read or wrote this table. |
| `--columns` | Also show which source columns each output column is derived from, recorded at deploy. Columns whose source can't be determined exactly are shown as opaque. |

### `trace`

```bash
duckstring trace POND.TABLE [--where PREDICATE] [-m N]
```

Finds which run produced some published rows: the newest run among the matching rows, with its version, timings, status, and the window of Source data it read.

| Argument / option | Description |
|---|---|
| `POND.TABLE` | The table. |
| `--where`, `-w` | A SQL condition selecting the rows, such as `"product_id = 7"`. Omit for the whole table. |

```bash
duckstring trace revenue.revenue_by_product --where "product_id = 7"
```

## Deleting

### `delete-table`

```bash
duckstring delete-table POND TABLE [--yes]
```

Deletes a table's published data and its state in the Pond's working database. It reappears only if the Pond's code still writes it on a later run. The Pond must be idle.

### `delete-object`

```bash
duckstring delete-object POND NAME [--yes]
```

Deletes a published Object. It reappears only if a Ripple writes it again.
