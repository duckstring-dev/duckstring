---
title: Freshness & Demand
description: The freshness-based Kanban that replaces schedules and DAG runs.
---

# Freshness & Demand

Duckstring has no scheduler, no cron, and no "DAG run". In their place is a single mechanism: every node carries a **freshness** timestamp, and **demand** signals flow through the graph like Kanban cards in a factory. This page is the working intuition; [Theory](../theory.md) is the precise spec.

## Freshness

Every Pond and Ripple has a freshness `F`: a timestamp meaning *"this node's output reflects the world as of `F`"*. Concretely, it's the run-start time of the oldest root (Inlet) feeding it — data is only as fresh as the oldest ingredient that went into it. A node that has never run has no freshness at all.

Two consequences do a lot of work:

- **A node should run when a Source is fresher than it** (`sourceF > startF`) — there's new input to consume. Comparing timestamps replaces all "did upstream finish?" bookkeeping.
- **Staleness is measurable**: how far behind *now* a node's freshness is. Operational requirements like "this table must never be more than an hour old" become a single number the runtime can act on.

When an Inlet's source updates in batches (say, daily), a [Window](../guides/windows.md) refines this: data is considered fresh until the end of the window it arrived in, so downstream nodes don't re-run pointlessly between batches.

A coordinated trigger stamps **one** freshness everywhere it reaches: a Pulse or Tap at time `T` lands `T` on every root it touches — even across a [duct](../guides/connecting-catchments.md) to another Catchment — so the whole cascade shares a freshness rather than drifting by when each node happened to run.

## Pull and push

Demand — the reason a node runs — comes in two flavours, and the difference is the direction the signal travels.

**Pull** is a request flowing *upstream*: "keep me supplied." A node holding a pull token runs whenever a Source is fresher than it, and *re-arms its Sources with new pull tokens when it starts* — so they're already preparing the next batch while it works. The signature behaviour: a continuously-pulled pipeline settles into the cadence of its slowest Ripple. Nothing upstream runs faster than its consumer can absorb, with no rate limit configured anywhere. (This is the Kanban property — work-in-progress is bounded by consumption, not by a schedule.)

**Push** is a target flowing *downstream*: "bring me to this freshness." Pushing a Pond stamps a target freshness on it, which propagates up its lineage; every node runs until its freshness meets the target, and the cascade arrives back at the pushed Pond. Push is simple and direct — the right tool when runs are occasional or on someone else's clock.

## The four triggers

The [trigger](../guides/triggers.md) surface is just these two flavours, each sent once or kept standing:

| | Once | Standing |
|---|---|---|
| **Push** | **Pulse** — run the lineage to *now* | **Tide** — keep staleness under a bound |
| **Pull** | **Tap** — one resupply | **Wave** — free-run at the bottleneck's pace |

A useful asymmetry: a Tide is a *staleness bound* ("never more than 30 minutes old"), not a schedule — the runtime decides when work must start to honour it. And a Wave isn't "run every N seconds" at all; its frequency is an emergent property of the pipeline's actual bottleneck.

Separately from triggers, the [control verbs](../guides/control.md) (Wake, Force, Sleep, Kill) operate on a single Pond without propagating demand.

## No change and passes

Freshness throttles a node to its upstream bottleneck, but a Source completing a run doesn't always mean its output *changed* — it may have recomputed to identical data. Re-running consumers in that case is wasted work. So a Pond tracks, separately from its freshness, **when its output last actually changed**. When a Pond is triggered but none of its Sources changed since it last ran, it **passes**: it completes instantly with no execution, advancing its freshness (so the demand heartbeat keeps flowing) while holding its content unchanged. Each downstream Pond then sees no change and passes in turn — "no change" propagates through the graph for free.

The effect is most visible under a standing Wave with nothing changing: only the **Inlets** keep executing — they alone can tell whether the outside world changed (and a [Window](../guides/windows.md) throttles even that to the batch cadence) — while the entire interior of the graph goes quiet, each Pond passing its freshness along without running. An Inlet, or any Pond doing a side effect, can also declare a no-change run explicitly with [`pond.skip()`](../reference/python-api.md#pondsources_changed--bool-and-pondskip).

## No concurrency cap

The Catchment never limits concurrent Pond Runs. If the pipeline takes 7 seconds end-to-end and the bottleneck cadence is 3 seconds, two to three runs are in flight at any moment, pipelined like instructions in a CPU. Flow control doesn't come from a cap — it comes from completions: a node only re-runs when its consumers have taken delivery and demanded more. Throughput is set by the bottleneck, latency by the critical path, and neither requires configuration.

## Why this replaces a scheduler

A schedule encodes a guess: "upstream will probably have new data by 2 a.m." Freshness encodes the fact: *this* data is from *this* time, and demand says who needs it fresher. The pipeline reacts to reality — late source data delays consumers rather than feeding them stale inputs, fast paths don't wait for slow ones, and the operator expresses intent (how fresh, by when) instead of mechanism (what runs at what time).
