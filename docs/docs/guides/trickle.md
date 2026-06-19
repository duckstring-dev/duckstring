---
title: Incremental Processing (Trickles)
description: Writing append and merge Trickles, reading deltas, the incremental-join builder, and retention.
---

# Incremental Processing

A [Trickle](../concepts/trickle.md) is a Ripple that preserves history, so consumers compose their output from only what changed. This guide is the how-to: declaring a Trickle, the two write modes, reading a Source's delta, the builder that composes an incremental join for you, and retention.

Scaffold the worked pipeline this guide draws from with:

```bash
duckstring pond demo --trickle
```

It creates `orders` (append) → `catalog` (merge) → `priced` (the builder) → `revenue` (an aggregate). `priced` and `revenue` ship Trickle-shaped Puddles, so `duckstring pond hydrate && duckstring pond run` runs them locally.

## There is no `@trickle`

A Trickle isn't a separate kind of node — it's a *table* a Ripple publishes with history preserved, and "incremental" is a capability any `@ripple` can reach for. You write a Trickle table by calling `pond.append_table` or `pond.merge_table` (instead of `write_table`) inside an ordinary Ripple; the merge key is declared at the write. One Ripple can publish plain overwrite tables and Trickle tables side by side — the mode is chosen per write, not per node.

## append — insert-only history

The fast path for event and fact logs whose identity is unique by construction. `append_table` strictly appends; there's no diff and no deletes. Each row is stamped with the run's freshness automatically.

```python
from duckstring import ripple


@ripple
def ingest(pond):
    batch = pond.con.sql("SELECT ... FROM new_orders")
    pond.append_table("order_line", batch, pk="order_id")
```

`pk` is optional on an append — it's recorded as the table's declared key (for downstream and the data viewer), but not enforced by default. Pass `validate_pk=True` to assert the key is unique across the appended rows and the existing history; it's a per-write cost that buys a correctness guarantee, and it raises before any write so the live table is untouched on a violation.

The single history table is at once the full read *and* the delta source. It is idempotent on replay — a retry or crash-recovery at the same freshness re-appends the same rows, never duplicates.

## merge — upsert with auto change-detection

When rows update or disappear, use `merge_table`. You hand it the *complete current state* and a `pk`, and Duckstring diffs it against the previous main — as a full-row **Z-set difference** — to derive the inserts, updates, and deletes for you. There's nothing to enumerate and no way to under-merge.

```python
@ripple
def ingest(pond):
    catalog = pond.con.sql("SELECT product_id, name, category, unit_price FROM ...")
    pond.merge_table("product", catalog, pk="product_id")   # full current state → Duckstring derives the CDC
```

`pk` is **required** for a merge — it's the merge identity. It accepts a single column or a tuple for a composite key.

This is correct by construction for any computation. The cost is a full recompute plus a diff each run; the *I/O* out is incremental (only the changed rows reach the changelog). When you want the *compute* incremental too — recomputing only the slice a change touches — use the [builder](#the-builder-pondtrickle), which composes source deltas through a join.

The result is two published tables: `product` (the clean current state, one row per key) and `product__changelog` (the Z-set CDC stream — an update is a `-1` of the old row and a `+1` of the new). A plain `read_table("catalog.product")` reads the clean main; a `read_delta` reads the changelog window.

## Reading a delta

A consuming Trickle reads a Source's change over its own window with `read_delta`. The change is a **Z-set** — `delta.zset` is the changed rows carrying the `_duckstring_d` weight column:

```python
@ripple
def enrich(pond):
    delta = pond.read_delta("orders.order_line")   # a Delta over (previous_f, f]
    ...
```

Most code doesn't touch `read_delta` directly — the [builder](#the-builder-pondtrickle) reads and composes deltas for you. When you do, a `Delta` exposes:

| | Meaning |
|---|---|
| `delta.zset` | the change as a Z-set — user columns plus `_duckstring_d` (`+1` present, `-1` retraction) |
| `delta.upserts` | derived: the net present rows (weight `> 0`), user columns only |
| `delta.deletes` | derived: the primary keys removed in the window |
| `delta.is_full` | `True` when this is a full read, not a window — see the note below |

For a **merge** Source the changelog window is consolidated by full row, so multiple updates and a delete-then-re-add within the window collapse to the net change, and re-reads are idempotent. For an **append** Source the window is the new rows (all `+1`). On a first run, when the consumer has fallen behind the Source's retention, or for a *changed* overwrite Ripple source, `read_delta` sets `is_full=True` and returns the whole current state — absorb that comprehensively (recompute the whole output and `merge_table`). An *unchanged* overwrite source returns an empty delta. The builder handles all of this for you.

## The builder: `pond.trickle(...)`

The builder is how you get an **incremental join**. It reads each Source's Z-set delta and composes them through the join (DBSP-style), recomputing only the output the change touches — and because it sees the whole graph, it can't forget an edge:

```python
@ripple
def priced_line(pond):
    (
        pond.trickle("orders.order_line")
            .join(pond.trickle("catalog.product"), on="product_id")
            .select(
                "s0.order_id, s0.product_id, s0.quantity, s1.unit_price, "
                "round(s0.quantity * s1.unit_price, 2) AS revenue"
            )
            .merge("priced_line", pk="order_id")
    )
```

The **spine** (the first source) is `s0`; joined dimensions are `s1`, `s2`, … in `.select(...)`. The chain:

- **`.join(pond.trickle(dim), on=…)`** — equi-join a dimension on **any** column(s). `on` is a shared column name (or list), or a `{spine_col: dim_col}` dict when the names differ. There's no FK=PK requirement: deletions are full-row retractions, so a change on any join key propagates soundly. Any table is a valid source — a Trickle or a plain overwrite Ripple.
- **`.filter(predicate)`** — a SQL boolean over the joined sources.
- **`.select(projection)`** — the output columns (required once there's a join); must include the PK. Computed columns are fine (`s0.a || '-' || s0.b AS key`).
- **`.merge(name, *, pk=, retain_t=, retain_n=)`** — execute. `pk` is **required**: the output identity (a column or tuple), which must be genuinely unique in the output.

When a dimension changes, the builder pre-filters the (large) spine to that dimension's changed join keys before the join — so a small dimension change doesn't drive a full spine scan. That key pre-filter is the general-purpose performance lever.

**Comprehensive fallback.** When a source can't supply a clean delta — a bootstrap, a coverage-miss, or a *changed* overwrite Ripple source — the builder recomputes the whole output and diffs it against the last-written main. It also takes that path when a source's delta exceeds its **change-fraction threshold** `p` (per source, default `0.3`): past that share of the source's rows, a clean full pass beats the incremental slice. Set `pond.trickle(ref, p=…)` to tune it (`p=1.0` disables the check for a source you know rarely matches the other side).

The op set is deliberately small — `join` / `filter` / `select` over sources — and **closed**: a snowflake chain (a dimension that itself has joins), a missing merge key, or a joined graph with no `.select` raises at *build time* rather than degrading silently. When you need something it won't express, do that part in a downstream Ripple.

A single-source transform (no join — just filter/project a stream) is the builder with no `.join()`: `pond.trickle("src.dim").select("id, upper(v) AS v").merge("loud", pk="id")`. For a shape outside the op set entirely, the low-level escape is `pond.apply_zset(name, zset, pk=…)` — apply a hand-built Z-set (user columns + `_duckstring_d`) directly — but that's rare; prefer a downstream node.

## Following a Ripple

A Trickle can read an ordinary (overwrite) Ripple as a source — the builder accepts it directly. While that Ripple is unchanged it's a free, stable join operand; the run a change lands, the consumer recomputes that output comprehensively (an overwrite source has no prior state to retract against, so the incremental win doesn't apply across that edge). Promote the Ripple to a Trickle upstream when you want its changes to flow incrementally — consumers don't change.

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

The full surface — `append_table`, `merge_table`, `apply_zset`, `read_delta`, and the builder — is in the [Python API reference](../reference/python-api.md#trickle-incremental-io).
