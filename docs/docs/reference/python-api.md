---
title: Python API
description: The duckstring package — the @ripple decorator and the Pond handle.
---

# Python API Reference

The surface a Pond author touches is intentionally tiny: one decorator to register Ripples, and one handle passed into every Ripple at runtime.

```python
from duckstring import ripple
```

## `@ripple`

Registers a function as a [Ripple](../concepts/ripples.md) of the Pond. The Catchment discovers a Pond's topology at deploy time by importing `src/pond.py` and reading these registrations.

```python
@ripple
def daily_sales(pond): ...

@ripple(parents=[daily_sales])
def join_lines(pond): ...

@ripple(parents=[daily_sales], name="join_lines_v2")
def join_lines_impl(pond): ...
```

| Parameter | Default | Meaning |
|---|---|---|
| `parents` | `[]` | Ripples (the decorated function objects) that must complete, within the same Pond Run, before this one starts. Ripples with no parent relationship run in parallel. All parent edges are required. |
| `name` | the function's name | The Ripple's registered name — used in topology, run history, and `duckstring get`/`query`. |

The decorated function must accept exactly one argument: the `Pond` handle. It returns nothing — output happens through `pond.write_table`. The decorator returns the function unchanged, so parents can reference it directly.

:::note
`src/pond.py` is imported both at deploy time (to read the topology) and at execution time. Keep module level free of side effects — work belongs inside Ripple bodies.
:::

## The `Pond` handle

Each Ripple invocation receives a fresh `Pond` — the runtime handle bound to the Pond's working database.

| Attribute | Type | Meaning |
|---|---|---|
| `pond.name` | `str` | The Pond's name |
| `pond.version` | `str` | The deployed version executing |
| `pond.con` | `duckdb.DuckDBPyConnection` | A connection to the Pond's private working database |
| `pond.root` | `Path` | The Catchment root (rarely needed directly) |

### `pond.read_table(ref)`

Returns a DuckDB **relation** for a table. The reference form decides where it reads from:

```python
own = pond.read_table("daily_sales")                  # this Pond's table — live, from the working DB
src = pond.read_table("transactions.transaction")     # a Source's table — its published Parquet snapshot
```

- **Bare name** — a table this Pond wrote, read live from its working database. This is how intermediate state flows between Ripples in a run (and how an Inlet can build on its own previous output).
- **`source_pond.table`** — a Source's *published* output: the Parquet snapshot exported by its last successful run. Reads never touch the Source's live database, so they see only consistent, completed data and never contend with the Source's execution.

Raises `FileNotFoundError` if a Source table has no exported snapshot yet — i.e. the Source hasn't completed a successful run.

### `pond.write_table(name, relation)`

Publishes a relation as a table of this Pond, atomically:

```python
agg = pond.con.sql("SELECT product_id, SUM(quantity) AS qty FROM raw GROUP BY 1")
pond.write_table("daily_sales", agg)
```

The write is build-then-swap: the relation materialises into a temporary table which then replaces the target in one transaction. Readers see the old table or the new one, never anything in between. Concurrent write conflicts (other Ripples writing their own tables to the same database) are retried with backoff automatically — they queue rather than fail.

Each successful Pond Run ends with every table exported to Parquet (`ponds/{pond}/data/{table}.parquet`) — that export is what Sinks and [queries](../guides/querying-data.md) consume.

### `pond.con` — direct DuckDB

`pond.con` is an ordinary DuckDB connection, with the full SQL and Python-API surface. The idiom worth knowing is **replacement scans**: DuckDB resolves table names in SQL against Python variables holding relations (or pandas/Polars/Arrow objects) in the enclosing scope:

```python
raw = pond.read_table("transactions.transaction")
agg = pond.con.sql("""
    SELECT product_id, SUM(quantity) AS total
    FROM raw                       -- the Python variable above
    GROUP BY product_id
""")
pond.write_table("totals", agg)
```

Relations are lazy — `pond.con.sql(...)` builds a query plan, and nothing executes until the result is consumed (here, by `write_table`). Chains of relations compose into a single optimised query.

Anything that produces a DuckDB relation works as `write_table` input, which is also the bridge for non-SQL transforms:

```python
import pandas as pd

df = pd.DataFrame(fetch_from_api())                    # arbitrary Python
pond.write_table("snapshot", pond.con.sql("SELECT * FROM df"))
```

## Execution environment

Facts about how Ripple code runs, occasionally relevant when writing it:

- **Threads, one process.** A Pond's Ripples execute in a thread pool inside the Pond's worker process. Independent Ripples genuinely overlap (DuckDB releases the GIL for query work); module-level mutable state in `pond.py` is shared and best avoided.
- **One database per Pond.** All of a Pond's Ripples share one working database; each invocation gets its own connection to it. Cross-Pond access is only ever via `read_table("source.table")` — Parquet snapshots, not the Source's database.
- **Failures are exceptions.** A Ripple fails by raising. The exception's message and traceback are captured into [run history](../guides/fault-tolerance.md), and the [immediate-retry budget](../guides/fault-tolerance.md#the-two-retry-budgets) governs re-attempts. Write Ripples idempotently — a retry re-runs the whole function. `write_table`'s replace semantics make the common derive-and-replace case idempotent by construction; append-style Ripples (which build on their own previous output) need their own care, since a retry appends again.
