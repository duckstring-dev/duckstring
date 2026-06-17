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

Registers a function as a [Ripple](../concepts/ripples.md) of the Pond. The Catchment discovers a Pond's topology at deploy time by importing `src/pond.py` (or the `ripples` path declared in [pond.toml](pond-toml.md)) and reading these registrations.

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
| `pond.f` | `datetime` | The run's [freshness](../concepts/freshness.md) (tz-aware UTC) — the natural watermark/provenance stamp, stable across retries and crash recovery (see [Incremental Ripples](../guides/incremental-ripples.md)) |
| `pond.previous_f` | `datetime` | The previous successfully-completed run's freshness — the lower bound of the bracket `(previous_f, f]` for hand-rolled incremental reads. Equal to the sentinel `NEVER` (far past) on the first run, so that bracket reads everything. Stable across retries/crash recovery, like `pond.f` |
| `pond.root` | `Path` | The Catchment root (rarely needed directly) |

### `pond.read_table(ref)`

Returns a DuckDB **relation** for a table. The reference form decides where it reads from:

```python
own = pond.read_table("daily_sales")                  # this Pond's table — live, from the working DB
src = pond.read_table("transactions.transaction")     # a Source's table — its published Parquet snapshot
```

- **Bare name** — a table this Pond wrote, read live from its working database. This is how intermediate state flows between Ripples in a run, and how a Ripple builds on its own previous output (see [Incremental Ripples](../guides/incremental-ripples.md)).
- **`source_pond.table`** — a Source's *published* output: the Parquet snapshot exported by its last successful run. Reads never touch the Source's live database, so they see only consistent, completed data and never contend with the Source's execution. The table is also registered as a view under its own name, so plain SQL can reference it directly — `FROM "transaction"` after the read above.

Raises `FileNotFoundError` if a Source table has no exported snapshot yet — i.e. the Source hasn't completed a successful run.

### `pond.write_table(name, relation)`

Publishes a relation as a table of this Pond, atomically:

```python
agg = pond.con.sql('SELECT product_id, SUM(quantity) AS qty FROM "transaction" GROUP BY 1')
pond.write_table("daily_sales", agg)
```

The write is build-then-swap: the relation materialises into a temporary table which then replaces the target in one transaction. Readers see the old table or the new one, never anything in between. Concurrent write conflicts (other Ripples writing their own tables to the same database) are retried with backoff automatically — they queue rather than fail.

Each successful Pond Run ends with every table published into the Pond's `data/` directory — via the [data plane](../guides/running-a-catchment.md#the-data-plane) (Iceberg by default, Parquet optional) — and that published copy is what Sinks and [queries](../guides/querying-data.md) consume.

Column names beginning with `_duckstring_` are **reserved** for framework system columns and rejected at publish time — keep your output columns out of that namespace.

### `pond.con` — direct DuckDB

`pond.con` is an ordinary DuckDB connection, with the full SQL and Python-API surface. SQL sees every table this Pond has written, plus a view for each Source table read with `read_table`:

```python
pond.read_table("transactions.transaction")    # registers the view `transaction`
agg = pond.con.sql("""
    SELECT product_id, SUM(quantity) AS total
    FROM "transaction"
    GROUP BY product_id
""")
pond.write_table("totals", agg)
```

Relations are lazy — `pond.con.sql(...)` builds a query plan, and nothing executes until the result is consumed (here, by `write_table`). Chains of relations compose into a single optimised query, and the relation API (`.filter`, `.aggregate`, `.union`, …) composes the same way.

One DuckDB feature to avoid inside Ripples: **replacement scans** — referencing a Python *variable* as a table name in SQL (``FROM raw`` for a local named ``raw``). That resolves by scanning Python stack frames, which is unreliable under the threaded executor Ripples run in. Reference registered names as above, or compose relations with the relation API instead.

Anything that produces a DuckDB relation works as `write_table` input, which is also the bridge for non-SQL transforms:

```python
import pandas as pd

df = pd.DataFrame(fetch_from_api())                    # arbitrary Python
pond.write_table("snapshot", pond.con.from_df(df))
```

## `@puddle` and the `Puddle` handle

Registers a function in `src/puddles.py` (or the `puddles` path in [pond.toml](pond-toml.md)) as a [Puddle](../guides/local-testing.md) — a local snapshot of the Source table it names, materialised by `duckstring pond hydrate`:

```python
from duckstring import puddle

@puddle("transactions.transaction")     # one table of a Source
def transactions(p):
    return p.con.sql("SELECT range AS id FROM range(50)")

@puddle("products")                     # a whole Source — name each table
def products(p):
    p.write_table("product", p.con.sql("SELECT 1 AS id"))
```

The handle `p`:

| Attribute | Meaning |
|---|---|
| `p.target` / `p.source` / `p.table` | The target as declared / its Source / its table (`None` for whole-Source puddles). |
| `p.con` | A scratch in-memory DuckDB connection. |
| `p.path` | The destination directory (`puddles/ponds/{source}/data/`) — write any non-table artifact there directly. |
| `p.write_table([name,] relation)` | Export a relation as a table's Parquet snapshot (atomic). Accepts anything `write_table` on a Pond accepts. |
| `p.write_path(path)` | Copy a parquet/csv file or glob in. |
| `p.catchment(name=None)` | A `Catchment` client bound to the Source: `.get([table])` fetches a table, `.query(sql)` runs SQL against the Source's exported tables, `.tables()` lists them. |

Returning a relation is shorthand for `p.write_table(relation)`; returning a path string for `p.write_path(path)`. Puddle code never runs on a Catchment — only `pond hydrate` imports it.

## Trickle: incremental I/O

A [Trickle](../concepts/trickle.md) is a history-preserving Ripple. Declare it with `@trickle` and write through `pond.append_table` / `pond.merge_table` instead of `write_table`; consumers read change-sets with `pond.read_delta`. The [Incremental Processing guide](../guides/trickle.md) is the worked walkthrough; this is the surface.

### `@trickle`

```python
from duckstring import trickle

@trickle(pk="order_id")
def ingest(pond): ...

@trickle(pk=("order_id", "line_no"), parents=[ingest])
def priced_line(pond): ...
```

| Parameter | Default | Meaning |
|---|---|---|
| `pk` | — | The output primary key (a column name or tuple) — identity for merge and for downstream delta reads. Required for a `merge_table`; the default a write inherits when it doesn't pass its own `pk=`. |
| `parents` | `[]` | As [`@ripple`](#ripple). |
| `name` | the function's name | As `@ripple`. |

A Trickle is orchestrated exactly like a Ripple — it differs only in I/O.

### `pond.append_table(name, relation, *, pk=None, retain_t=None, retain_n=None)`

Append `relation` to the insert-only history table `name`; each row is stamped with `pond.f`. No diff, no uniqueness check, no deletes. Idempotent on replay at the same freshness. The history table is both the full read and the delta source.

### `pond.merge_table(name, relation, *, comprehensive=True, deletes=None, pk=None, retain_t=None, retain_n=None)`

Upsert `relation` into the clean current-state **main** table `name`, recording the changes in its `__changelog` companion.

| Parameter | Default | Meaning |
|---|---|---|
| `comprehensive` | `True` | `relation` is the *complete* current state — Duckstring diffs it against the previous state to derive inserts/updates/deletes. The safe default. |
| `deletes` | `None` | Only with `comprehensive=False`: a relation (or `KeySet`) of primary keys to remove. |
| `pk` | the `@trickle` default | The merge identity. |
| `retain_t` / `retain_n` | `None` | Bound the kept changelog: a `timedelta` and/or a run count. Off by default (keep everything); a lag SLA, never a correctness gate. |

With `comprehensive=False`, `relation` is a *partial* set of changed rows. **Under-supplying changes or deletes silently corrupts data** — prefer the default, or the builder below.

### `pond.read_delta(ref)` → `Delta`

A Source's change-set over this run's window `(pond.previous_f, pond.f]`. Resolves the Source's mode automatically (append history window; merge changelog collapsed per key; an overwrite Ripple → a full read), and falls back to a full read on a first run or a coverage miss.

| Attribute / method | Meaning |
|---|---|
| `delta.upserts` | The changed rows (new + updated), user columns only — a DuckDB relation. |
| `delta.deletes` | The removed primary keys — a relation. |
| `delta.keys()` | `upserts ∪ deletes` as a `KeySet`. |

### The builder — `pond.trickle(spine_ref)`

A fluent builder over Trickle sources that wires an incremental join and *can't forget an edge* (it sees the whole graph). Chain `.join(pond.trickle(dim), on=…)` / `.filter(predicate)` / `.select(projection)`, then `.merge(name, *, pk=None, retain_t=None, retain_n=None)`. The spine owns the output key; dimensions are `s1`, `s2`, … in the projection. The op set is closed — unsupported operations raise at build time. See the [guide](../guides/trickle.md#the-builder-pondtrickle).

### Partial-path helpers

For `comprehensive=False` by hand. They operate on key-sets, imposing no compute layer.

| Helper | Returns |
|---|---|
| `pond.keys_joining(spine_ref, delta, on=…)` | The spine keys a dimension `delta` ripples to (`on` equi-joins the spine to the dimension's full key; a non-key join is rejected). A `KeySet`. |
| `pond.affected_groups(delta, by=…)` | The group keys a `delta` touches, for re-aggregation. A `KeySet`. |
| `KeySet.union(other)` | The union, deduplicated. |
| `KeySet.dropped(recomputed)` | The deletes — affected keys absent from `recomputed` (a relation). |
| `KeySet.create_view(name)` / `KeySet.relation` | Register as a view / the underlying relation. |

## Execution environment

Facts about how Ripple code runs, occasionally relevant when writing it:

- **Threads, one process.** A Pond's Ripples execute in a thread pool inside the Pond's worker process. Independent Ripples genuinely overlap (DuckDB releases the GIL for query work); module-level mutable state in `pond.py` is shared and best avoided.
- **One database per Pond.** All of a Pond's Ripples share one working database; each invocation gets its own connection to it. Cross-Pond access is only ever via `read_table("source.table")` — Parquet snapshots, not the Source's database.
- **Failures are exceptions.** A Ripple fails by raising. The exception's message and traceback are captured into [run history](../guides/fault-tolerance.md), and the [immediate-retry budget](../guides/fault-tolerance.md#the-two-retry-budgets) governs re-attempts. Write Ripples idempotently — a retry re-runs the whole function. `write_table`'s replace semantics make the common derive-and-replace case idempotent by construction, and the same atomicity makes self-read appends replay-safe when the increment is computed from the previous state (see [Incremental Ripples](../guides/incremental-ripples.md)). What needs care is anything with *external* side effects — a retry repeats them (see [External Pipelines](../guides/external-pipelines.md) for the ensure-then-poll shape).
