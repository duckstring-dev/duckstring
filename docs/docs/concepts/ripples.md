---
title: Ripples
description: The execution units within a Pond.
---

# Ripples

A **Ripple** is a single unit operation inside a [Pond](ponds.md) — usually one transformation producing one table. Where the Pond is the unit of ownership and versioning, the Ripple is the unit of execution: it's Ripples that actually run, retry, and report durations.

## Declaring Ripples

Ripples are ordinary Python functions in the Pond's `src/pond.py`, registered with the `@ripple` decorator. Each takes a single `pond` argument — the runtime handle it uses to read and write tables:

```python
from duckstring import ripple


@ripple
def daily_sales(pond):
    pond.read_table("transactions.transaction")    # registers the view `transaction`
    agg = pond.con.sql("""
        SELECT product_id, created_at AS sale_date,
               SUM(quantity) AS total_quantity, COUNT(*) AS tx_count
        FROM "transaction"
        GROUP BY product_id, created_at
    """)
    pond.write_table("daily_sales", agg)


@ripple
def price_tiers(pond):
    ...


@ripple(parents=[daily_sales, price_tiers])
def join_lines(pond):
    sales = pond.read_table("daily_sales")
    tiers = pond.read_table("price_tiers")
    ...
```

`parents` declares the intra-Pond dependencies: `join_lines` runs only after `daily_sales` and `price_tiers` have completed within the same Pond Run. Independent Ripples run in parallel. All intra-Pond dependencies are required — there are no optional edges inside a Pond.

The full handle API — `read_table`, `write_table`, `pond.con` for arbitrary DuckDB SQL — is documented in the [Python API reference](../reference/python-api.md).

## Reading across the Pond boundary

A Ripple addresses its own Pond's tables by bare name (`"daily_sales"`) and a Source's tables as `"source_pond.table"` (`"transactions.transaction"`). The two reads are deliberately different things:

- **Own tables** are read live from the Pond's private DuckDB registry — intermediate state flowing between Ripples within the run.
- **Source tables** are read from the Source's published snapshot — the output of its last successful run, via the [data plane](../guides/running-a-catchment.md#the-data-plane). A Ripple never reaches into another Pond's internals.

## How Ripples execute

When the Catchment starts a Pond Run, the Pond's worker executes every Ripple to the run's freshness, walking the intra-Pond graph: roots first, each Ripple starting as soon as all of its parents have finished. Each Ripple's wall-clock span is recorded in [run history](../guides/web-ui.md), and a failing Ripple — after its [immediate retries](../guides/fault-tolerance.md) — fails the Pond Run with its error and traceback attached.

Ripples are also the resolution at which the pull model operates. Demand propagates Ripple-to-Ripple, not just Pond-to-Pond — which is why a continuously-pulled pipeline throttles itself to the slowest *Ripple*, not the slowest Pond. [Freshness & Demand](freshness.md) explains the mechanics.

## The incremental variant: Trickles

A Ripple overwrites its tables wholesale each run. When you'd rather preserve history — so a consumer reads only what changed — declare it as a [**Trickle**](trickle.md) instead, with `@trickle`. It's a Ripple in every orchestration respect (a node in the graph, run and retried the same way); it differs only in its I/O, writing an append history or a merge changelog rather than a full overwrite. Trickles and Ripples mix freely in one pipeline.

## Granularity

A good Ripple is one logical output: a table and the transformation that produces it. Splitting work into Ripples buys parallelism (independent Ripples run concurrently), precise retry scope (a retry re-runs the failed Ripple, not the whole Pond), and a legible run history. Work that always changes together belongs in one Ripple; work that can usefully run, fail, or be timed separately belongs in separate ones.
