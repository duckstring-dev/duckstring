---
title: duckstring puddle
description: Inspect local Puddles and run output.
sidebar_label: puddle
---

# duckstring puddle

Commands for inspecting a Pond project's local data: the Puddles written by [`pond hydrate`](pond.md#hydrate) and the output of [`pond run`](pond.md#run). They run against the `puddles/` directory of the current project, in an in-memory DuckDB connection, and need no Catchment.

Tables are available as `"{pond}"."{table}"`, or by bare name when unambiguous. Where the run output has a table with the same name as one in the Pond's own Puddle, the output is used.

## `ls`

```bash
duckstring puddle ls
```

Lists hydrated Puddles and run output tables, with row counts, sizes and ages.

## `show`

```bash
duckstring puddle show REF [--limit N]
```

Prints the first rows of a table.

| Argument / option | Default | Description |
|---|---|---|
| `REF` | | `{pond}.{table}`, or a bare table name. |
| `--limit`, `-n` | `10` | Rows to show. |

## `query`

```bash
duckstring puddle query SQL
```

Runs SQL over every Puddle and output table and prints the result.

```bash
duckstring puddle query 'SELECT category, SUM(revenue) FROM "sales"."sale_line" GROUP BY 1'
```
