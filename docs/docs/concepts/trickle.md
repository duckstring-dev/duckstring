---
title: Trickles
description: The incremental variant of a Ripple — history-preserving I/O and Z-set incremental joins.
---

# Trickles

A **Trickle** is a [Ripple](ripples.md) that works **incrementally**. An ordinary Ripple overwrites its tables wholesale each run; a Trickle *preserves history*, so a downstream consumer can read only the rows that changed since it last ran — a small **delta out** instead of a full table.

In every orchestration respect a Trickle *is* a Ripple: it's a node in the package graph, it runs, retries, and reports the same way, and [freshness and demand](freshness.md) flow through it identically. The only difference is its I/O.

## What a Trickle is — and is not

A Trickle delivers incremental I/O and transfer, **incremental joins**, and **incremental aggregation**. Every change is carried as a **Z-set** — each row tagged with an integer weight (`+1` for a present row, `-1` for a retraction) — and the [builder](../guides/trickle.md#the-builder-pondtrickle) composes the Z-set deltas of its sources through joins and aggregations, so when only some inputs change it recomputes just the affected output rather than the whole result. Because a deletion is a full-row `-1` (not a key-only tombstone), a join can be on **any** column and deletes still propagate soundly.

Aggregation is maintained from the delta too: `count`/`sum`/`mean`/`min`/`max`/`var`/`stddev`, the weighted family, and two-variable co-moments (covariance/correlation/OLS) all update only the groups a change touches, and order-dependent scans (`.accumulate(...)`) resume from carried state. The boundary is the **holistic** aggregates — `DISTINCT`, median, percentile — which need retraction-aware operators Duckstring doesn't ship; those stay a downstream comprehensive step that re-runs over its full input. That's the honest boundary, and the right one at Duckstring's single-node target (transforms up to roughly 50M rows): the bytes a transform *publishes* and *ships between Catchments* are usually what hurt, and a Trickle shrinks those regardless; incremental joins and aggregation then cut the in-between work for the shapes that have it.

## Two modes

A Trickle is either **append** or **merge** — history-preserving in two different shapes. The mode is chosen by which write method you call, and it travels with the published data so consumers resolve it automatically.

| Mode | Write | Shape | For |
|---|---|---|---|
| **append** | `pond.append_table(...)` | One insert-only history table | Event / fact logs whose identity is unique by construction — no diff, no deletes |
| **merge** | `pond.merge_table(...)` | A clean current-state **main** + an append-only **changelog** (CDC stream) | Dimensions and any computed state where rows update or disappear |

A merge Trickle keeps its `main` table as the clean current state — one row per primary key, no tombstones, so a plain read of it is just "the data now". Alongside it, a `__changelog` companion records each run's change as a **Z-set**: an update is a `-1` of the old row plus a `+1` of the new, a delete a `-1` of the old row, an insert a `+1` of the new. You hand `merge_table` the *complete current state* and Duckstring **derives that Z-set for you** by diffing it against the previous main — see the [guide](../guides/trickle.md).

## Freshness in the data

The mechanism under both modes is the run's [freshness](freshness.md) stamped into every history row, in a framework-owned `_duckstring_f` column. A consumer reads the window `(previous_f, f]` — both bounds from its *own* freshness, no per-edge watermark — as a plain content predicate (`WHERE _duckstring_f > previous_f AND _duckstring_f <= f`). Because the bound lives *in the data* and is never a storage cursor, the window read is correct regardless of how the history is compacted, and falls back to a full read whenever the window can't be covered (a first run, or a consumer that's been away longer than the producer's retention). Correctness never depends on retention; it's a lag SLA, not a gate.

## Composition

Incrementality chains through **Trickle → Trickle**, and any table is a valid Source for the builder:

- A **Trickle reading a Trickle** composes its Z-set delta and stays incremental.
- A **Trickle reading an ordinary Ripple** (overwrite output) treats it as a Source like any other. If that Ripple *hasn't* changed since the consumer last ran it costs nothing (a stable join operand); if it *has* changed, the consumer recomputes that output comprehensively (an overwrite source carries no prior state to retract against). Promote the Ripple to a Trickle upstream to make its changes incremental too — a zero-touch change for consumers.
- A **Ripple reading a Trickle** reads its clean current state, like any other Source.

So you can adopt Trickles gradually: turn the nodes that benefit into Trickles and leave the rest as Ripples; mixing the two is expected, not a special case.

## Where to go next

- **[Incremental processing](../guides/trickle.md)** — the guide: writing append and merge Trickles, reading deltas, the `pond.trickle(...)` builder, and retention.
- **[Incremental Ripples](../guides/incremental-ripples.md)** — the same idea by hand, with `pond.f` / `pond.previous_f`, for when you want the watermark logic explicit (or a shape a Trickle doesn't cover).
- **[Python API](../reference/python-api.md)** — the exact write/read surface.
