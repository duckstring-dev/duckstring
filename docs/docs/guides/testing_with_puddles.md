---
title: Testing with Puddles
description: Run a Pond on your machine against sample data before deploying it.
---

# Testing with Puddles

Deploying to find out whether a Ripple works is slow. A Puddle is a code-defined stand-in for a Source table: generated sample data, or the real Source read from a Catchment, whole or filtered. `duckstring pond run` runs your Pond against its Puddles on your machine, with no Catchment, in seconds.

The loop is:

```bash
duckstring pond hydrate     # build the Puddles
duckstring pond run         # run the Pond against them
duckstring pond puddle ls        # look at what came out
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

A returned relation is written as the target table, and a returned path is copied in as a file. `p.con` is an in-memory DuckDB connection for building the data; make any scratch tables on it `TEMP`.

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

## Testing against real data

Sample data shows that a Ripple runs. Whether its numbers mean anything usually needs the real data. `p.read_source()` reads the Source's published table in place, on the Catchment you hydrate from, so a Puddle can be a filter over the real thing:

```python
@puddle("transactions.transaction")
def transactions(p):
    return p.read_source().filter("store_id IN (1, 2)")
```

The table is read straight from the Catchment's files: from its root when the Catchment runs on this machine and is registered with it, or from its data root (an S3 bucket, say) with your own credentials. Only what the filter keeps is read and written.

For a large Source, copying even a filtered sample can be slow. `duckstring pond hydrate --live` copies nothing:

```bash
duckstring pond hydrate --live
duckstring pond run
```

A Source with no Puddle is then read in place whole, and a Puddle that uses `read_source` is evaluated when the run needs it. The hydration time is recorded, and runs read each live Source as it stood then, so results stay close to stable until you hydrate again. Nothing on the Catchment changes because you are reading it. If the Catchment has since dropped a version you were reading, the oldest one it keeps stands in, with a warning. When the data must not move under you, copy it instead (without `--live`).

`--as-at` hydrates as of an earlier time, which is useful for reproducing a result someone saw yesterday.

## Puddle sets

A Pond often wants more than one set of Puddles: a tiny one for checking that the code runs, and a real week for checking the numbers. Label definitions with `set`:

```python
@puddle("transactions.transaction")
def transactions(p):
    return p.con.sql("SELECT ...")             # the default set

@puddle("transactions.transaction", set="week")
def transactions_week(p):
    return p.read_source().filter("created_at >= DATE '2026-11-10'")
```

`duckstring pond hydrate --set week` uses the `week` definitions, and the default set's for any Source or table `week` doesn't define. To pick a set per Source, list them in a `puddles.toml` beside `pond.toml`:

```toml
[local]
transactions = "week"
```

`puddles.toml` isn't part of the Pond's version, so it can change as often as you like, and can stay out of version control.

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

`hydrate --source products` rebuilds only one Source's Puddles. Hydrating sets the baseline that every plain `pond run` starts from.

## Inspecting results

```bash
duckstring pond puddle ls
duckstring pond puddle show sales.sale_line --limit 20
duckstring pond puddle query 'SELECT category, SUM(revenue) FROM "sales"."sale_line" GROUP BY 1'
```

Every Puddle and output table is available as `"{pond}"."{table}"`, or by bare name. This is also a quick way to check a Source's shape while writing SQL against it.

## Testing incremental Ponds

A Ripple that reads changes with `read_delta` or the Trickle builder behaves differently on its first run than on later ones. A plain `pond run` is always a baseline run: it starts from an empty `puddles/out/` and reads every Source in full, which tests the first-run path. `pond run --incremental` goes on from the last run, as a Refresh would on a Catchment, so its Ripples read only what changed in between:

```bash
duckstring pond hydrate --as-at 2026-11-17T00:00:00Z
duckstring pond run                        # the baseline, as of the 17th
duckstring pond run --incremental          # the next change after it
duckstring pond run --incremental          # and the one after that
duckstring pond run                        # back to the baseline
```

The changes come from one of two places.

**Real history.** `--as-at` copies each Source's state at that time and every change since (a Puddle that uses `read_source` is run at each change, so the copy holds only what it keeps). With `--live`, a Source's history is read in place instead, and keeps growing as the Catchment runs, so incremental runs can follow production forward.

**Steps.** A new pipeline has no history to replay. `@puddle.step` writes a Source's next window instead:

```python
@puddle("orders.order_line")
def orders(p):
    p.append_table(p.con.sql("SELECT range AS order_id, 1 AS quantity FROM range(100)"), pk="order_id")


@puddle.step("orders.order_line")
def more_orders(p):
    p.append_table(p.con.sql(f"SELECT {p.step} * 1000 + range AS order_id, 2 AS quantity FROM range(10)"),
                   pk="order_id")
```

A step runs when the target lies beyond the history a Source's Puddle holds, so steps also carry a replay on past the end of real data. Seed anything random with `p.step`, so a rerun from the baseline writes the same rows. Write a Trickle Source's Puddle with `append_table` or `merge_table`, as the Source itself does, so a Pond reading its deltas sees each step's change. A step that writes nothing still counts as a run of that Source. Steps apply only to copied Puddles.

### Choosing the next window

Like a Refresh, an incremental run first brings every Source up to a target freshness. The default, `--next-change`, is the next time any Source changed. `--next-run` replays the windows the Pond itself ran in production, `--most-recent` goes as far as every Source has reached, and `--to` names a time. Each Source is then read as it stood when the last of them got there, and the run's freshness is the oldest of theirs, as it would be on a Catchment.

A copied Source runs out of history at some point. From then on only a step can bring it further, and once no Source has history left, steps are stamped with the current time. That is a jump in time: a Ripple working relative to `pond.f` ("the last 24 hours") or a retention window sees it. A step can give its rows timestamps within its own window (`p.previous_f` to `p.f`) to keep them plausible.

Replaying a definition through history, and following a live one, both compare what it gives at one position with what it gave at the last. A definition has to give the same rows for the same data: a filter, a join, or a sample by a hash of the key, never a random sample.

### The Pond's own previous output

A Puddle can also target the Pond's own name. `pond run` copies it into place before a baseline run, so the baseline starts from a prior state rather than nothing:

```python
@puddle("priced")
def previous_output(p):
    p.write_path("~/samples/priced/*.parquet")
```

`duckstring pond run --fresh` ignores this Puddle and starts from nothing.

## What a local run doesn't do

A local run is a Pond Run of one Pond. It doesn't involve retries, alerts or other Ponds, and an incremental run's target stands in for a Refresh. `pond.sources_changed()` always returns `True` and `pond.skip()` does nothing.

`pond.changed()` works on the Pond's own tables and Objects as it does in a deployed run, so you can check that a Ripple asking it before `pond.skip()` sees what you expect. Asked about a Source, it reports `True` on a baseline run, and in an incremental run whether the Source changed in the window. A baseline run starts from an empty `puddles/out/`, so `pond.detect_change` and `if_changed=True` have nothing published to compare with, and always write. For the full behaviour, deploy to a Catchment on your own machine; see [Local Execution](../concepts/management_and_execution.md#local-execution).

The `puddles/` directory is for your machine only. The scaffolded `.gitignore` keeps it out of version control, and `.pondignore` keeps it out of deployments.
