---
title: Trickles
description: The incremental variant of a Ripple — history-preserving I/O, not incremental compute.
---

# Trickles

A **Trickle** is a [Ripple](ripples.md) that works **incrementally**. An ordinary Ripple overwrites its tables wholesale each run; a Trickle *preserves history*, so a downstream consumer can read only the rows that changed since it last ran — a small **delta out** instead of a full table.

In every orchestration respect a Trickle *is* a Ripple: it's a node in the package graph, it runs, retries, and reports the same way, and [freshness and demand](freshness.md) flow through it identically. The only difference is its I/O.

## What a Trickle is — and is not

A Trickle delivers **incremental I/O and incremental transfer, not incremental computation.** Joins still recompute fully each run — you cannot derive `Δ(A ⋈ B)` from input deltas without maintaining per-operator state, and that machinery (with its worst-in-data failure mode, silently wrong results) is exactly what Duckstring keeps out of the core. So a Trickle's win is the **small write and small draw** at the boundary, not less work in the middle.

That's the honest scope, and it's the right one at Duckstring's single-node target (transforms up to roughly 50M rows): the bytes a transform *publishes* and *ships between Catchments* are usually what hurt, and those are what a Trickle shrinks.

## Two modes

A Trickle is either **append** or **merge** — history-preserving in two different shapes. The mode is chosen by which write method you call, and it travels with the published data so consumers resolve it automatically.

| Mode | Write | Shape | For |
|---|---|---|---|
| **append** | `pond.append_table(...)` | One insert-only history table | Event / fact logs whose identity is unique by construction — no diff, no deletes |
| **merge** | `pond.merge_table(...)` | A clean current-state **main** + an append-only **changelog** (CDC stream) | Dimensions and any computed state where rows update or disappear |

A merge Trickle keeps its `main` table as the clean current state — one row per primary key, no tombstones, so a plain read of it is just "the data now". Alongside it, a `__changelog` companion records the per-run inserts, updates, and deletes that a delta read consumes. With `merge_table`'s default, Duckstring **derives that changelog for you** by diffing the new state against the previous one — see the [guide](../guides/trickle.md).

## Freshness in the data

The mechanism under both modes is the run's [freshness](freshness.md) stamped into every history row, in a framework-owned `_duckstring_f` column. A consumer reads the window `(previous_f, f]` — both bounds from its *own* freshness, no per-edge watermark — as a plain content predicate (`WHERE _duckstring_f > previous_f AND _duckstring_f <= f`). Because the bound lives *in the data* and is never a storage cursor, the window read is correct regardless of how the history is compacted, and falls back to a full read whenever the window can't be covered (a first run, or a consumer that's been away longer than the producer's retention). Correctness never depends on retention; it's a lag SLA, not a gate.

## Composition

Incrementality chains through **Trickle → Trickle**:

- A **Trickle reading a Trickle** reads its delta and stays incremental.
- A **Trickle reading an ordinary Ripple** (overwrite output) full-reads at that hop and merges comprehensively — perfectly correct, just not incremental across that one edge. A chain stays incremental for the Trickle→Trickle runs after the overwrite node.
- A **Ripple reading a Trickle** reads its clean current state, like any other Source.

So you can adopt Trickles gradually: turn the nodes that benefit into Trickles and leave the rest as Ripples; mixing the two is expected, not a special case.

## Where to go next

- **[Incremental processing](../guides/trickle.md)** — the guide: writing append and merge Trickles, reading deltas, the `pond.trickle(...)` builder, retention, and the partial-merge escape hatch.
- **[Incremental Ripples](../guides/incremental-ripples.md)** — the same idea by hand, with `pond.f` / `pond.previous_f`, for when you want the watermark logic explicit (or a shape a Trickle doesn't cover).
- **[Python API](../reference/python-api.md)** — the exact write/read surface.
