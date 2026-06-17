---
title: Incremental Processing (Trickles)
description: Writing append and merge Trickles, reading deltas, the builder, retention, and the partial path.
---

# Incremental Processing

A [Trickle](../concepts/trickle.md) is a Ripple that preserves history, so consumers read only what changed. This guide is the how-to: declaring a Trickle, the two write modes, reading a Source's delta, the builder that wires an incremental join for you, retention, and the expert partial path.

Scaffold the worked pipeline this guide draws from with:

```bash
duckstring pond demo --trickle
```

It creates `orders` (append) → `catalog` (merge) → `priced` (the builder) → `revenue` (an aggregate). `priced` and `revenue` ship Trickle-shaped Puddles, so `duckstring pond hydrate && duckstring pond run` runs them locally.

## Declaring a Trickle

Use `@trickle` instead of `@ripple`, and declare the output **primary key** — the identity Duckstring uses for merge and for downstream delta consumption:

```python
from duckstring import trickle


@trickle(pk="order_id")
def ingest(pond):
    ...
```

`pk` accepts a single column or a tuple for a composite key. Everything else about the function is a normal Ripple: `parents=`, `name=`, the `pond` handle.

## append — insert-only history

The fast path for event and fact logs whose identity is unique by construction. `append_table` strictly appends; there's no diff, no uniqueness check, and no deletes. Each row is stamped with the run's freshness automatically.

```python
@trickle(pk="order_id")
def ingest(pond):
    batch = pond.con.sql("SELECT ... FROM new_orders")
    pond.append_table("order_line", batch)
```

The single history table is at once the full read *and* the delta source. It is idempotent on replay — a retry or crash-recovery at the same freshness re-appends the same rows, never duplicates.

## merge — upsert with auto change-detection

When rows update or disappear, use `merge_table`. By default it is **comprehensive**: you hand it the *complete current state*, and Duckstring diffs it against the previous state to derive the inserts, updates, and deletes for you.

```python
@trickle(pk="product_id")
def ingest(pond):
    catalog = pond.con.sql("SELECT product_id, name, category, unit_price FROM ...")
    pond.merge_table("product", catalog)   # comprehensive: Duckstring derives the CDC
```

This is correct by construction for any computation, including joins — you never have to enumerate what changed. The cost is a full recompute plus a diff each run; the *I/O* is incremental (only the changed rows are written to the changelog), the compute is not. That trade-off is the whole point — see [what a Trickle is and is not](../concepts/trickle.md#what-a-trickle-is--and-is-not).

The result is two published tables: `product` (the clean current state, one row per key) and `product__changelog` (the CDC stream). A plain `read_table("catalog.product")` reads the clean main; a `read_delta` reads the changelog window.

## Reading a delta

A consuming Trickle reads a Source's change-set over its own window with `read_delta`:

```python
@trickle(pk="order_id")
def enrich(pond):
    delta = pond.read_delta("orders.order_line")   # a Delta over (previous_f, f]
    out = delta.upserts.project("order_id, product_id, quantity")
    pond.merge_table("enriched", out, comprehensive=False, deletes=delta.deletes)
```

A `Delta` exposes three relations:

| | Meaning |
|---|---|
| `delta.upserts` | the changed rows (new + updated), user columns only |
| `delta.deletes` | the primary keys removed in the window |
| `delta.keys()` | `upserts ∪ deletes` as a key-set — the changed identities |

For a **merge** Source the changelog window is collapsed per key to the latest operation, so a delete-then-re-add within the window resolves to *present*, and re-reads are idempotent. For an **append** Source the window is the new rows. On a first run, or when the consumer has fallen behind the Source's retention, `read_delta` transparently falls back to a full read.

## The builder: `pond.trickle(...)`

Writing a correct partial merge by hand means enumerating *every* edge a change can ripple through — miss one and you silently under-merge. For the common shapes, the builder does that bookkeeping for you and **can't forget an edge**, because it sees the whole join graph:

```python
@trickle(pk="order_id")
def priced_line(pond):
    (
        pond.trickle("orders.order_line")
            .join(pond.trickle("catalog.product"), on="product_id")
            .select(
                "s0.order_id, s0.product_id, s0.quantity, s1.unit_price, "
                "round(s0.quantity * s1.unit_price, 2) AS revenue"
            )
            .merge("priced_line")
    )
```

`.merge()` reads each Source's delta, propagates the affected order keys along every join edge (a new order line, *or* an order line whose product price changed), recomputes just that slice from the full Sources, and writes it. The **spine** (the first source) owns the output primary key; joined dimensions are addressed as `s1`, `s2`, … in `.select(...)`.

The op set is deliberately small — `join` / `filter` / `select` over Trickle sources — and **closed**: anything outside it (a non-equi or self join, a non-key join column, a snowflake chain through an intermediate, a joined graph with no `.select`) raises at *build time* rather than silently degrading to a full refresh. When you need something the builder won't express, do that part in a downstream Ripple or Trickle.

## The partial path by hand

The builder is sugar over three primitives you can use directly when a shape falls outside it. They operate on **key-sets only**, so your transform stays raw SQL:

```python
@trickle(pk=("order_id", "line_no"))
def priced_line(pond):
    ol = pond.read_delta("sales.order_line")
    pr = pond.read_delta("catalog.product")
    pond.read_table("sales.order_line")          # full spine, view `order_line`
    pond.read_table("catalog.product")           # full dimension, view `product`

    # Which output keys does a change touch? The spine's own changed keys, plus the spine keys a
    # changed product ripples to. keys_joining sees deletes too, so a dropped dimension propagates.
    affected = ol.keys().union(pond.keys_joining("sales.order_line", pr, on="product_id"))
    affected.create_view("affected")

    recomputed = pond.con.sql("""
        SELECT ol.order_id, ol.line_no, ol.product_id, p.unit_price,
               ol.qty * p.unit_price AS line_total
        FROM order_line ol JOIN affected USING (order_id, line_no)
        JOIN product p USING (product_id)
    """)

    pond.merge_table("priced_line", recomputed,
                     comprehensive=False, deletes=affected.dropped(recomputed))
```

- **`delta.keys()`** — the changed keys of a delta.
- **`pond.keys_joining(spine, delta, on=…)`** — the spine keys a dimension change ripples to. `on` equi-joins the spine to the dimension's **full primary key** (a non-key join is rejected — it's what keeps delete propagation sound).
- **`affected.dropped(recomputed)`** — the deletes: keys that were affected but fell out of the recompute. Because `.keys()` folds in source deletes, a removed spine row lands here automatically — the case that's easy to miss by hand.

For aggregations there's a sibling, `pond.affected_groups(delta, by=…)` — the group keys a delta touches, to re-aggregate just those groups from the full input.

:::warning Comprehensive is the safe default
With `comprehensive=False` you own correctness: **over-merging** (re-emitting unchanged rows) is harmless — idempotent merge absorbs it — but **under-merging** (missing a changed row, or under-supplying `deletes`) is silent data corruption. Reach for the partial path only when the full recompute is genuinely too expensive, and prefer the builder, which can't forget an edge.
:::

## Following a Ripple

A Trickle can sit downstream of an ordinary (overwrite) Ripple. Read its output with `read_table` and write a **comprehensive** merge — the diff infers the deletes from the overwrite snapshot:

```python
@trickle(pk="id")
def loud(pond):
    pond.read_table("plain_ripple.dim")          # full read of the overwrite Source
    pond.merge_table("loud", pond.con.sql("SELECT id, upper(v) AS v FROM dim"))
```

The incremental win simply doesn't apply across that one edge. (The builder and the delta helpers need a Trickle source — they raise a guiding error over an overwrite one, pointing you here.)

## Retention

A Trickle's history and changelog grow run over run. `retain_t` (a `timedelta`) and `retain_n` (a run count) bound them, trimmed at write time:

```python
from datetime import timedelta

pond.merge_table("product", state, retain_t=timedelta(days=30))
```

Retention is a **lag SLA, not a correctness control**: a consumer (or a downstream Catchment) that has been away longer than the retained window simply full-reads the current state and resumes incrementally. Longer or unbounded retention is the opt-in for audit and replay. It defaults to off (keep everything).

## Across Catchments

When a downstream Catchment [draws](connecting-catchments.md) a Trickle Source, the transfer is incremental too: it ships only the changelog rows newer than what it has already landed, and merges them in. The mode/key metadata travels with the data, so the consuming Catchment resolves the Source without any shared configuration. A first draw, or one past retention, transfers the current state whole.

## Reference

The full surface — `@trickle`, `append_table`, `merge_table`, `read_delta`, the builder, and the helpers — is in the [Python API reference](../reference/python-api.md#trickle-incremental-io).
