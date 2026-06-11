---
title: Puddles
description: Local snapshots of a Pond's Sources — how a Pond is tested before deployment.
---

# Puddles

A **Puddle** is a code-defined snapshot of a Source table, materialised locally so a [Pond](ponds.md) can run before it's deployed. The package boundary that makes Ponds independently ownable also means a Pond's Sources usually live in someone else's repository — there is nothing for `read_table("transactions.transaction")` to read until the Pond sits next to real data. Puddles stand in for that data: one Puddle per Source table, holding whatever the test needs it to hold.

## Declaring Puddles

Puddles are ordinary Python functions in the Pond's `src/puddles.py`, registered with the `@puddle` decorator — the same shape as [Ripples](ripples.md), but emulating an input rather than producing an output. A definition is just code that puts data in the Puddle's location; the three common shapes are synthesising it, copying it from a file, and pulling a sample from a [Catchment](catchment.md):

```python
from duckstring import puddle


@puddle("transactions.transaction")
def transactions(p):
    return p.con.sql("SELECT range AS id, range % 10 AS product_id FROM range(50)")


@puddle("products.product")
def products(p):
    return "~/data/product_sample.parquet"


@puddle("stores.store")
def stores(p):
    p.write_table(p.catchment().get())
```

The target names the Source table being emulated, and must belong to a Source declared in `pond.toml`. Puddles are deliberately untyped — there is no "synthetic Puddle" or "file Puddle", only code with a destination, so anything Python can produce can stand in for a Source.

## Why the Pond's code doesn't change

Hydrating materialises each Puddle to `puddles/ponds/{source}/data/{table}.parquet` — the same layout a Catchment root uses for published Source output. A local run simply points the Pond's runtime handle at `puddles/` instead of a Catchment root, and every `read_table` call resolves exactly as it would in production. The transform under test is the transform that ships; there is no test harness dialect.

The run's output lands separately in `puddles/out/` — a Pond's own output is a result, not a Puddle.

## The self-Puddle

One special case: a Puddle targeting *the Pond itself* is a snapshot of its **prior state**, for testing incremental Ripples that build on their own previous output. When one exists, every full local run first seeds the output area from it, then executes — computing prior-state + input → next-state. Because the seed is re-copied each run, running twice gives the identical result: increments stay deterministic and re-testable.

## What Puddles are not

Puddles are a development-time affordance, not part of the runtime. They never deploy — a Catchment only ever imports the Pond's Ripples — and a local run against them is a single Pond Run with no [freshness, demand](freshness.md), or scheduling involved. The workflow (`pond hydrate`, `pond run`, `puddle ls/show/query`) is covered in [Local Testing](../guides/local-testing.md).
