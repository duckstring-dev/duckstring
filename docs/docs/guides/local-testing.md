---
title: Local Testing
description: Test a Pond before deployment with Puddles — local snapshots of its Sources.
---

# Local Testing

A Pond's Sources usually live in someone else's repository, so the transform code has nothing to read until it's deployed next to real data. [**Puddles**](../concepts/puddles.md) close that gap: a Puddle is a code-defined snapshot of a Source table, materialised locally, that the Pond can run against — no Catchment, no deploy.

The loop is two commands in the project root:

```bash
duckstring pond hydrate     # materialise the Source snapshots
duckstring pond run         # execute the Pond against them
```

## Define Puddles

Puddles live in `src/puddles.py`, one `@puddle` per Source table the Pond reads. A definition is just code that puts data in the puddle's location — synthesise it, copy it from a file, or pull it from a Catchment:

```python
from duckstring import puddle


@puddle("transactions.transaction")
def transactions(p):
    return p.con.sql("SELECT range AS id, range % 10 AS product_id, 1 AS quantity FROM range(50)")


@puddle("products.product")
def products(p):
    return "~/data/product_sample.parquet"          # a path is copied in


@puddle("stores.store")
def stores(p):
    p.write_table(p.catchment().get())              # pulled from the default Catchment
```

The target names the Source table the Puddle emulates, and must belong to a Source declared in `pond.toml`. The handle `p` carries:

| Attribute | Meaning |
|---|---|
| `p.con` | A scratch in-memory DuckDB connection. |
| `p.write_table(relation)` | Export a relation as the target table's snapshot. With `p.write_table(name, relation)`, a whole-Source puddle (`@puddle("transactions")`) names each table it emits. |
| `p.write_path(path)` | Copy a parquet/csv file (or glob) in. |
| `p.catchment(name=None)` | A client for a registered Catchment: `.get()` fetches the target table, `.query(sql)` runs SQL against the Source's exported tables. |
| `p.path` | The destination directory itself — write anything there directly (models, blobs, non-table artifacts). |

Returning a relation or a path from the function is shorthand for `write_table`/`write_path`.

## Hydrate

```bash
duckstring pond hydrate                                   # hydrate every defined puddle
duckstring pond hydrate -s transactions -s products       # only these Sources
```

`hydrate` materialises each definition into `puddles/ponds/{source}/data/{table}.parquet` — the same layout a Catchment root uses, which is why the Pond's `read_table` calls work unchanged. With no flags it hydrates **all** of the project's puddles; `--source`/`-s` (repeatable) restricts it to specific Sources, useful for refreshing one snapshot without re-pulling the rest.

A declared Source with no puddle definition is **skipped with a warning**; pass `--from-catchment` to fill those gaps with the Source's exported tables from the Catchment instead (`-c` selects which Catchment, for this and for `p.catchment()` puddles). Hydration is offline by default — the network is only touched by puddles that ask for it.

The `puddles/` directory is plain visible Parquet, and gitignored by the `pond init` scaffold.

## Run

```bash
duckstring pond run                       # the whole Pond, in dependency order
duckstring pond run --ripple join_lines   # one Ripple, against the last run's state
```

A full run resets `puddles/out/`, executes every Ripple in topo order, and exports the Pond's tables to `puddles/out/{table}.parquet`. On a failure it stops, prints the traceback, and exits non-zero — the local equivalent of the run detail view.

This is a **single local Pond Run**, not the orchestration model: no freshness, no triggers, no Ducks. It answers "does my transform produce the right tables from this input", nothing more.

## Inspect

```bash
duckstring puddle ls                                          # everything local: rows, size, age
duckstring puddle show transactions.transaction               # preview a table
duckstring puddle query 'SELECT * FROM "sales"."sale_line"'   # SQL across snapshots + output
```

Snapshots register as `"{source}"."{table}"`, the run's output under the Pond's own name — so the query surface mirrors `duckstring query` against a real Catchment.

## Test incremental behaviour

An append-style Ripple builds on its own previous output, which an overwriting run can't exercise. Define a puddle for *the Pond itself* — its prior state:

```python
@puddle("sales.sale_line")
def prior_output(p):
    return p.con.sql("SELECT * FROM read_parquet('fixtures/prior_sale_line.parquet')")
```

When a self-puddle exists, every full run first **seeds** `puddles/out/` from it before executing, so the run computes prior-state + new-input → next-state. Because the seed is re-copied each time, running twice produces the identical result — increments stay testable and deterministic. Pass `--fresh` to ignore the seed and start from nothing.

## Custom entrypoints

The defaults are `src/pond.py` and `src/puddles.py`; both are declarable in `pond.toml`:

```toml
[pond]
name = "sales"
version = "1.0.0"
ripples = "transforms/main.py"
puddles = "transforms/snapshots.py"
```
