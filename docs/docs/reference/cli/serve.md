---
title: duckstring serve
description: Query the catalog and control what it exposes.
sidebar_label: serve
---

# duckstring serve

Commands for the Catchment's catalog: read-only SQL across every Pond, which tables users with read-only access can see, and which major version a bare schema name refers to.

In catalog SQL, each Pond is a schema. `{pond}_v{major}` refers to a specific major line, and a bare `{pond}` refers to its served major: the first major deployed, until another is promoted.

```sql
SELECT * FROM reports_v2.monthly_summary;   -- major version 2
SELECT * FROM reports.monthly_summary;      -- the served major
```

Users with a read-level key see only exposed tables, in an isolated DuckDB that can't read files or network locations. Users with full access see every table.

Every command takes `-c` / `--catchment`.

## `query`

```bash
duckstring serve query SQL [--limit N]
```

Runs read-only SQL across the catalog and prints the result. `--limit` / `-n` caps the printed rows (default `1000`).

## `status`

```bash
duckstring serve status
```

Lists each Pond with its served major, its deployed majors, and its exposed tables.

## `expose`

```bash
duckstring serve expose POND TABLE (--on | --off | --default) [-m N]
```

Shows or hides a table from read-level users, overriding the Pond's [`[serve] tables`](../pond_toml.md#serve).

| Option | Description |
|---|---|
| `--on` | Expose the table. |
| `--off` | Hide the table. |
| `--default` | Remove the override and follow `pond.toml`. |

## `promote`

```bash
duckstring serve promote POND --major N
```

Makes major version `N` the one a bare `{pond}` schema refers to, switching every consumer of the bare name at once. Refused if that major doesn't publish every table currently served.
