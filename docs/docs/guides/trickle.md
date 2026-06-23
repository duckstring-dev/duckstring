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

`pk` is optional on an append — it's recorded as the table's declared key (for downstream and the data viewer). When set, `fail_on_conflict=True` (the default) asserts the key is unique across the appended rows and the existing history, raising before any write so the live table is untouched on a violation. Pass `fail_on_conflict=False` for the trust-the-writer fast path (no check) when the key is unique by construction; with `pk` unset the check is a no-op either way.

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

The **spine** (the first source) is `s0`; joined dimensions are `s1`, `s2`, … in `.select(...)` — *or* give each source a name with **`.alias()`** and reference that instead (recommended once there's more than one join). The chain:

- **`.alias(name)`** — name a source so `.select`/`.filter` read `o.order_id` / `p.unit_price` instead of `s0`/`s1`. `s0`/`s1` stay the fallback for unaliased sources. Aliasing also makes the select *reorder-safe* — the name follows the source, so reordering `.join()` calls (which you might do for cost) no longer silently remaps positional references.
- **`.join(pond.trickle(dim), on=…, how="inner")`** — equi-join another operand on **any** column(s). `on` is a shared column name (or list), or a `{left_col: right_col}` dict when the names differ (qualify a bare name as `alias.col` if it's ambiguous across sources). There's no FK=PK requirement: deletions are full-row retractions, so a change on any join key propagates soundly. Any table is a valid source — a Trickle or a plain overwrite Ripple. `how` ∈ `inner` / `left` / `right` / `full` / `semi` / `anti` — **all maintained incrementally**, including the outer joins' NULL-padded incomparables (reference those columns with care in `.select`). The operand may itself be a join DAG, so bushy `(a⋈b)⋈(c⋈d)` and snowflake shapes compose directly (each binary join is maintained by an affected-key recompute, with no privileged spine).
- **`.filter(predicate)`** — a SQL boolean over the joined sources.
- **`.select(projection)`** — the output columns (required once there's a join); must include the PK. Computed columns are fine (`o.a || '-' || o.b AS key`).
- **`.merge(name, *, pk=, retain_t=, retain_n=)`** — execute. `pk` is **required**: the output identity (a column or tuple), which must be genuinely unique in the output.

Here the same join with aliases:

```python
(pond.trickle("orders.order_line").alias("o")
     .join(pond.trickle("catalog.product").alias("p"), on="product_id")
     .select("o.order_id, o.quantity, p.unit_price, round(o.quantity * p.unit_price, 2) AS revenue")
     .merge("priced_line", pk="order_id"))
```

When one side of a join changes, the builder pre-filters **both** inputs to that side's changed join keys before the join — so a small change doesn't drive a full scan of the other side. That key pre-filter is the general-purpose performance lever, applied per binary join node.

**Comprehensive fallback.** When a source can't supply a clean delta — a bootstrap, a coverage-miss, or a *changed* overwrite Ripple source — the builder recomputes the whole output and diffs it against the last-written main. It also takes that path when a source's delta exceeds its **change-fraction threshold** `p` (per source, default `0.3`): past that share of the source's rows, a clean full pass beats the incremental slice. Set `pond.trickle(ref, p=…)` to tune it (`p=1.0` disables the check for a source you know rarely matches the other side).

**Strategy escapes** (rarely needed — measure first). Two flags on `.merge()`/`.append()`, both default `True`, let you override the engine's choices for a specific build without dropping to raw SQL: `ivm=False` ignores deltas entirely and recomputes the whole output with plain full-table joins, diffed against the stored main — the escape for when the incremental machinery turns out counterproductive. `key_filter=False` keeps the incremental delta but skips the key pre-filter — for when the change is large enough to trip `p` anyway, so filtering buys nothing.

The op set is deliberately small — `join` / `filter` / `select` over sources — and **closed**: a builder method exists *only* when the engine maintains it incrementally. A join operand carrying its own `.filter()`/`.select()`/`.aggregate()`/`.sql()`, a missing merge key, an ambiguous join key, or a joined graph with no `.select` raises at *build time* rather than degrading silently. Anything outside that set — aggregation, window functions, `DISTINCT`, set ops — goes through **`.sql()`** (below).

A single-source transform (no join — just filter/project a stream) is the builder with no `.join()`: `pond.trickle("src.dim").select("id, upper(v) AS v").merge("loud", pk="id")`. For a shape outside the op set entirely, the low-level escape is `pond.apply_zset(name, zset, pk=…)` — apply a hand-built Z-set (user columns + `_duckstring_d`) directly — but that's rare; prefer `.sql()` or a downstream node.

### Beyond the op set: `.sql()`

`.sql(query)` is the comprehensive escape hatch. Name the builder with `.alias()`, then run any SQL over it:

```python
priced = (
    pond.trickle("orders.order_line").alias("o")
        .join(pond.trickle("catalog.product").alias("p"), on="product_id")
        .select("o.product_id, o.quantity, round(o.quantity * p.unit_price, 2) AS revenue")
        .merge("priced_line", pk="order_id")          # ← incremental join, cached
)
(
    priced.alias("pl")
          .sql("SELECT product_id, sum(revenue) AS total_revenue, count(*) AS orders "
               "FROM pl GROUP BY product_id")
          .merge("revenue_by_product", pk="product_id")   # ← aggregate, then delta out
)
```

This is the whole `priced → revenue` pipeline in one Ripple. Two things to understand about `.sql()`:

- It **breaks incremental compute** — after `.sql()` the data is fully materialised; there are no joins, key pre-filter, or fast-path shortcuts left (`.join`/`.select`/`.filter` after it raise). Aggregation isn't in the DBSP core, so the `GROUP BY` *does* re-scan `priced_line` each run — the same cost the separate `revenue` Pond pays. The `.merge()` before `.sql()` is load-bearing: it's what keeps the *join* incremental.
- It **keeps incremental output** — the terminal `.merge()` still diffs the aggregate against last run's totals, so only products whose revenue actually moved reach the changelog. The win is the small delta *out*, not less compute in.

So `.sql()` collapses the `priced→revenue` Ripple boundary into one node — an *ergonomic* win (one deployment unit), not a compute one. Keep them as separate Ponds when `priced_line` is a reuse/parallelism boundary (which is why the demo does).

**Ibis.** If you have [Ibis](https://ibis-project.org) installed, hop into it without Duckstring depending on it: `.to_ibis_schema()` returns a schema dict `ibis.table(...)` accepts, and `.sql()` takes an Ibis expression directly (compiled lazily to DuckDB SQL):

```python
import ibis
pl = ibis.table(priced.to_ibis_schema(), name="pl")
agg = pl.group_by("product_id").aggregate(total_revenue=pl.revenue.sum())
priced.alias("pl").sql(agg).merge("revenue_by_product", pk="product_id")
```

### Incremental aggregation

The `.sql()` aggregate above re-scans the whole input each run (aggregation is comprehensive there). For the **distributive / algebraic** aggregates, `.aggregate()` maintains the result *incrementally* — only the groups a change touches are recomputed:

```python
from duckstring import agg

(pond.trickle("orders.order_line").alias("o")
     .join(pond.trickle("catalog.product").alias("p"), on="product_id")
     .select("o.product_id, o.quantity, round(o.quantity * p.unit_price, 2) AS revenue")
     .aggregate(by="product_id",
                total_revenue=agg.sum("revenue"),
                units=agg.sum("quantity"),
                orders=agg.count(),
                avg_revenue=agg.mean("revenue"),
                top_price=agg.max("unit_price"),
                revenue_sd=agg.stddev("revenue"))
     .merge("revenue_by_product"))   # a merge Trickle keyed by `by`; pk defaults to product_id
```

- **Metrics** are [`duckstring.agg`](../reference/python-api.md#aggregate-metrics) specs, not SQL — `count` / `sum` / `mean` / `min` / `max` / `var` / `stddev`, the weighted family (`weight_total` / `weighted_sum` / `weighted_average`), two-variable co-moments (`covariance` / `pearson_correlation` / `ols_slope` / `ols_intercept`), payload extremes (`argmin` / `argmax`) and boolean/bitwise reductions (`bool_and` / `bool_or` / `bit_and` / `bit_or`). `.group_by(by).aggregate(**metrics)` is the same operator, Ibis-shaped.
- **Incremental in *and* out.** Raw accumulators (count, per-column running sum + non-NULL count + sum-of-squares; per extreme column a stored min/max) live in a registry-only companion; a new order or a reprice updates only the affected product's accumulators (O(δ)), and only the products whose values moved reach the changelog. Contrast `.sql()`, whose `GROUP BY` re-scans every run. The one non-O(δ) case is `min`/`max` when the supporting row is *retracted* — that group rescans its current membership (an append-only stream never does).
- **Terminal-bound to `.merge()`** — `pk` defaults to `by`; `.append()` or a further join/select after `.aggregate()` is out of the op set (do it downstream). Anything outside count/sum/mean still goes through `.sql()`.

### Chaining through materialised intermediates

`.merge(name, pk=…)` **returns a builder rooted at the table it just wrote**, so you can keep joining — materialising intermediates mid-chain, all in one Ripple:

```python
ab = (
    pond.trickle("a.order_line")
        .join(pond.trickle("b.product"), on="product_id")
        .select("s0.order_id, s0.qty, s1.category")
        .merge("ab", pk="order_id")          # ← stores `ab`, returns a handle
)
(
    ab.join(pond.trickle("c.tax"), on="category")   # joins on `category`, produced by a⋈b
      .select("s0.order_id, s0.qty, s0.category, s1.tax")
      .merge("abc", pk="order_id")
)
```

Why materialise rather than write one big `a.join(b).join(c)`? A single builder keeps **no stored intermediate**, so a run that changes only `c` still joins through `a⋈b`. The mid-chain `.merge("ab")` stores `ab`'s trace (its main + changelog), so that run reuses the stored `ab` and never touches `a`/`b`. It's the same win as splitting into a downstream Trickle — every Trickle boundary materialises a reusable history — **without the second Ripple's boilerplate**. The chain is also where you put a join whose key only exists *after* an upstream join (here `category`), which a single star builder can't express.

Two things to know:

- **It's sequential by construction.** Statements in a Ripple run top-to-bottom on one connection. The only thing a separate Ripple buys you over a chain is *parallelism* — two independent branches running concurrently under a Wave. A chain is the right tool when the steps depend on each other; reach for separate Ripples when they don't and you want them to overlap.
- **Materialise at reuse boundaries, not between every join.** Each `.merge()` is a real registry write. Scatter them where the stored trace earns its keep (a hop reused across runs, or a join key that only exists downstream), the same judgement as deciding where to split a Trickle.

The returned handle is the next builder's **spine** — its just-computed delta is threaded forward in memory (nothing is published mid-run, so a downstream join can't re-read it by name). Passing a composed builder as a *dimension* (`x.join(ab, …)`) is still rejected — dimensions must be bare sources; keep the composed thing on the spine.

### Appending the builder's output

`.merge(name, pk=…)` maintains a clean main + changelog — correct for any transform, including ones whose output rows update or disappear. When the transform is **monotonic** — output rows are only ever *added*, never updated or retracted, like enriching an append-only fact stream with stable/SCD dimensions — `.append(name, pk=…)` writes an insert-only history instead:

```python
(
    pond.trickle("orders.order_line")
        .join(pond.trickle("catalog.product"), on="product_id")
        .select("s0.order_id, s0.product_id, s0.qty, s1.unit_price")
        .append("enriched", pk="order_id")
)
```

An insert-only table can't reflect a *change to the past*, so two things are conflicts: a **retraction** in the computed delta (a previously-emitted row changed or disappeared), and a `+1` row whose `pk` is already in history with a **different** image. (A `+1` whose `pk` is in history with an *identical* image is a benign idempotent skip — never a conflict.) The `fail_on_conflict` flag (default `True`, correctness first) decides what happens:

- **`True`** — raise on any conflict. Use this when the transform *should* be monotonic and a conflict means a bug (or a dimension you assumed stable isn't).
- **`False`** — drop the offending rows (history wins, the past stays frozen) and append the rest. With `log_drops=True` (the default) the dropped rows land in a `{name}__droplog` companion — published alongside the table like a merge's `__changelog`, growing one record per run (the output's columns + the Z-set weight + the run freshness); set `log_drops=False` to skip even that if collisions are expected and you don't care.

`pk` is optional but recommended — it's what the conflict check keys on. `pk=None` with `fail_on_conflict=False` skips the checks entirely: the fastest path, sound only when you're certain duplicates and changed-pasts are impossible by construction. Like `.merge()`, `.append()` returns a chainable handle.

#### The spine-PK fast path

When the output is keyed by the **spine's own PK** — projected straight through (`s0.order_id`, optionally aliased) — *and* you've set both `fail_on_conflict=False` and `log_drops=False`, the builder takes a shortcut. In that mode a change to an already-appended row is dropped-and-forgotten regardless of what caused it, so a **dimension** change to an existing fact can't affect the result. The builder skips the dimension deltas entirely and computes only `new spine rows ⋈ current dimensions`. A dimension reprice that touches a million existing facts becomes a no-op instead of a million-row scan; a new fact is enriched with the dimension's *current* value.

This is automatic and conservative — it engages only when the PK is a verbatim `s0.<col>` pass-through of the spine's declared key, and falls back to the full path for anything computed or renamed-off-a-dimension (the full path is always correct, so a missed detection only costs speed, never correctness). It's the in-builder equivalent of "this fact stream is enrich-once, append-only" — exactly the case where recomputing against changing dimensions would be wasted work.

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
