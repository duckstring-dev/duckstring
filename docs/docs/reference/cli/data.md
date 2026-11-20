---
title: status, runs, query, get and data
description: Watch Ponds and their Runs, query and download what they publish, and manage their tables and Objects.
sidebar_label: status, runs, query, get, data
---

# status, runs, query, get and data

Commands for watching a Catchment and its Runs, and for reading, exposing, tracing and deleting what its Ponds publish.

Tables and Objects are named `POND.NAME`, as in the Python API, for example `reports.monthly_summary`. A part that contains dots goes in backticks: ``forecasting.`model.pkl` ``. When a Pond has a table and an Object with the same name, add `--table` or `--object`.

Every command takes `-c` / `--catchment`, and those acting on one Pond take `-m` / `--major` and usually `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `status`

```bash
duckstring status [POND] [-m N | -v VERSION] [--once | --live] [--json]
```

Shows every Pond's state (running, queued, idle, failed, blocked or killed), freshness and recurring Refreshes, updating every second until `Ctrl+C`. When its output isn't a terminal (a script, a pipe, an agent), it prints one snapshot and exits.

| Argument / option | Description |
|---|---|
| `POND` | Only show this Pond and the Ponds upstream of it. |
| `--once` | Print one snapshot and exit. |
| `--live` | Keep updating even when the output isn't a terminal. |
| `--json` | Print one snapshot as JSON, with every field [`GET /api/status`](../http_api.md) gives for each Pond: the version running, each Ripple's state, `rebuild_pending`, memory slots held, the last error and more. |

## `runs`

```bash
duckstring runs [POND] [--upstream] [--ripples] [--limit N] [-m N] [--json]
```

Lists recent Pond Runs, newest first: the Pond and version, the Run's freshness, its status, when it started, how long it took and its error. A Run is `running`, `success`, `failed`, `killed` or `superseded`: a superseded Run never finished because a later Run did its work, and its detail names that Run.

| Argument / option | Default | Description |
|---|---|---|
| `POND` | every Pond | Only this Pond's Runs. |
| `--upstream` | | With `POND`, also the Runs of the Ponds upstream of it. |
| `--ripples` | | List each Run's Ripple Runs under it, one row per attempt, with retries and errors. |
| `--limit`, `-n` | `20` | How many Pond Runs, at most 1000. |
| `--json` | | Print the Runs as JSON, as [`GET /api/runs`](../http_api.md) gives them. Tracebacks are included for a full-access key. |

## `query`

```bash
duckstring query SQL [--pond POND] [--at TIME [--since TIME]] [--limit N] [--csv FILE | --json FILE | --parquet FILE]
                     [--path DIR]
duckstring query POND.TABLE [--at TIME [--since TIME]]
```

Runs read-only SQL and prints the result, or saves it to a file. Given a table instead of SQL, it shows the table's first 10 rows.

SQL runs across the catalog, where each Pond is a schema: `reports` refers to the Pond's served major, and `reports_v2` to a specific one. Users with a read-level key see only exposed tables. With `--pond`, the SQL runs against that Pond's tables, which can then be named without the schema.

```bash
duckstring query reports.monthly_summary
duckstring query "SELECT r.month, r.total_revenue FROM reports_v1.monthly_summary r ORDER BY 1"
duckstring query "SELECT category, SUM(revenue) FROM sale_line GROUP BY 1" --pond sales
duckstring query @queries/top_categories.sql --csv top.csv
```

`--at` reads one Pond's tables as they stood at a freshness: each plain table at its newest version at or before it, and each Trickle at its rows up to it. `--since` as well gives the change in the result between the two times instead: the rows only `--at` gives, weighted `+1` in a `_duckstring_d` column, and those only `--since` gives, weighted `-1`. A row that appears twice at `--at` and once at `--since` is one `+1` row.

```bash
duckstring query "SELECT * FROM sale_line WHERE store_id = 7" --pond sales --at 2026-06-16T06:00
duckstring query "SELECT * FROM sale_line WHERE store_id = 7" --pond sales --at 2026-06-16T07:00 --since 2026-06-16T06:00
```

A Pond keeps only part of its history, so a read can reach only so far back. A Trickle that holds nothing from before the time asked for (created later, or trimmed or compacted since) is refused, naming the earliest time it holds. A plain table usually keeps only its newest version once its Pond is idle, so a plain table whose version at that time is gone is read at its newest, with a warning.

| Argument / option | Description |
|---|---|
| `SQL` | The query, or `@path/to/file.sql` to read it from a file. |
| `POND.TABLE` | A table, to show its first rows. |
| `--pond`, `-p` | Run the SQL against this Pond's tables, by bare name. |
| `--at` | Read the Pond's tables as they stood at this time (ISO 8601, UTC unless it gives an offset). Needs `--pond` or `POND.TABLE`. |
| `--since` | With `--at`: the change in the result since this time. |
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
duckstring data ls [POND] [-m N] [--json]
```

Lists each Pond's published tables and Objects: whether each table is exposed to read-level users (and whether that comes from `pond.toml` or an override), and when each last changed. Without `POND`, lists every Pond. `-m` picks a major version; the default is the served major. `--json` prints the same as JSON.

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
