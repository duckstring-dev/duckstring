---
title: Testing with Puddles
description: Run a Pond on your machine against sample data before deploying it.
---

# Testing with Puddles

Deploying to find out whether a Ripple works is slow, and running against full production data is slower still. A Puddle is a small, code-defined sample of a Source table. `duckstring pond run` runs your Pond against its Puddles on your machine, with no Catchment, in seconds.

The loop is:

```bash
duckstring pond hydrate     # build the Puddles
duckstring pond run         # run the Pond against them
duckstring puddle ls        # look at what came out
```

## Defining Puddles

Puddles live in `src/puddles.py`. Each is a function decorated with `@puddle`, naming the Source table it stands in for. The function builds the data and either writes it or returns it:

```python
from duckstring import puddle


@puddle("products.product")
def product(p):
    return p.con.sql("""
        SELECT * FROM (VALUES
            (1, 'Laptop Pro',       'Electronics', 1299.00),
            (2, 'Wireless Earbuds', 'Electronics',   89.99),
            (3, 'Espresso Beans',   'Food',          14.99)
        ) t(id, name, category, unit_price)
    """)
```

A returned relation is written as the target table, and a returned path is copied in as a file. `p.con` is a scratch DuckDB connection.

Keep generated data deterministic, so a test gives the same answer every time. The demo `sales` Pond seeds its random generator for exactly this reason:

```python
import random
from datetime import date, timedelta


@puddle("transactions.transaction")
def transactions(p):
    rng = random.Random(42)
    rows = ", ".join(
        f"({i}, DATE '{date.today() - timedelta(days=rng.randint(0, 89))}', "
        f"{rng.randint(1, 3)}, {rng.randint(1, 8)}, {rng.randint(1, 5)})"
        for i in range(50)
    )
    return p.con.sql(f"SELECT * FROM (VALUES {rows}) t(id, created_at, product_id, quantity, store_id)")
```

### Other ways to fill a Puddle

Copy an existing file, or a glob of them, with `write_path`:

```python
@puddle("products.product")
def product(p):
    p.write_path("~/samples/products.parquet")
```

Sample from a running Catchment with `p.catchment()`, a [client](../reference/python/catchment.md) already pointed at the Source:

```python
@puddle("transactions.transaction")
def transactions(p):
    return p.catchment().query('SELECT * FROM "transaction" USING SAMPLE 1000 ROWS')
```

A whole Source can be one Puddle, naming each table it writes:

```python
@puddle("products")
def products(p):
    p.write_table("product", p.catchment().get("product"))
    p.write_table("supplier", p.catchment().get("supplier"))
```

A Source with no Puddle is skipped with a warning when you hydrate. `duckstring pond hydrate --from-catchment` fills those Sources with a full copy of their tables from the default Catchment instead, which is convenient for small Sources.

## Running

```bash
duckstring pond hydrate
duckstring pond run
```

`hydrate` writes each Puddle to `puddles/ponds/{source}/data/`, laid out the way a Catchment publishes data, so `pond.read_table("products.product")` finds it unchanged. `run` then executes every Ripple once, one at a time in dependency order, writing the output to `puddles/out/`. It stops at the first failing Ripple and prints its traceback.

To work on one step, rerun just that Ripple against the output already there:

```bash
duckstring pond run --ripple join_lines
```

`hydrate --source products` rebuilds only one Source's Puddles.

## Inspecting results

```bash
duckstring puddle ls
duckstring puddle show sales.sale_line --limit 20
duckstring puddle query 'SELECT category, SUM(revenue) FROM "sales"."sale_line" GROUP BY 1'
```

Every Puddle and output table is available as `"{pond}"."{table}"`, or by bare name. This is also a quick way to check a Source's shape while writing SQL against it.

## Testing incremental Ponds

A Ripple that reads changes with `read_delta` or the Trickle builder behaves differently on its first run than on later ones. By default every `pond run` starts from an empty `puddles/out/`, so it always tests the first-run path.

To test a later run, give the Pond a Puddle of its own previous output. A Puddle can target the Pond's own name, and `pond run` copies it into place before running, so each run starts from the same prior state:

```python
@puddle("priced")
def previous_output(p):
    p.write_path("~/samples/priced/*.parquet")
```

`duckstring pond run --fresh` ignores this Puddle and starts from nothing.

A Source that is itself a Trickle needs its change history, not just a table, for `read_delta` to return a window of changes. A plain table Puddle, or one copied from a Catchment with `get()`, is read as a full table, which tests the full-recompute path. To test the incremental path, build the Source Puddle with the Trickle write functions, as the demo `priced` Pond does in `src/puddles.py` (`duckstring pond demo --trickle`).

## What a local run doesn't do

A local run is a single Pond Run of one Pond. It doesn't involve freshness, triggers, retries or other Ponds. `pond.sources_changed()` always returns `True` and `pond.skip()` does nothing. For the full behaviour, deploy to a Catchment on your own machine; see [Local Execution](../concepts/management_and_execution.md#local-execution).

The `puddles/` directory is for your machine only. The scaffolded `.gitignore` excludes it from version control.
