---
title: Control
description: Wake, Sleep, Force, Kill — operating a Pond directly.
---

# Control

Where [triggers](triggers.md) put demand into the graph and let it propagate, the control verbs act on **one Pond, without propagating**. They're the operator's hands-on tools — for nudging a Pond after a patch, quieting part of the graph, or stopping something that's gone wrong.

```bash
duckstring control {wake|force|sleep|kill|clear} {pond}
```

All verbs accept `-c` for the Catchment and `-m`/`-v` to target a specific major or version. `wake` and `force` open the live status view until the Pond settles (`--silent` to skip; `--watch` to keep it open).

## Wake — run if there's new input

```bash
duckstring control wake sales
```

Wake gives a Pond a single, **non-propagating** pull: it runs once when its Sources already hold fresher data — and *waits* for that if they don't yet — without soliciting the Sources to produce anything. Compare a [Tap](triggers.md), which sends pull all the way upstream; Wake consumes what exists.

It's the gentle nudge: "catch up on whatever's there." Waking a [failed or killed](fault-tolerance.md) Pond also clears that state.

## Force — recompute now

```bash
duckstring control force sales
```

Force re-runs the Pond immediately **at its current freshness** — no upstream change required. Its inputs are the same, so its freshness doesn't advance, and therefore nothing downstream re-runs: the recompute is contained to this Pond.

That containment is the point. The canonical use is after deploying a patch: the transform logic changed but the input data didn't, so you want *this* Pond's output rebuilt without replaying the lineage or waking the consumers. (If downstream *should* see the change — say the fix alters the published tables — follow up with a `wake` on the consumer, or let its standing demand pick the change up naturally.) Force also clears a failed/killed state.

## Refresh — rebuild from scratch on the next run

```bash
duckstring control refresh sales      # ...and `--clear` to un-flag
```

Force *recomputes* the current state; **refresh** *rebuilds it from nothing*. It flags the Pond so its **next** run wipes the working database and reads its Sources in full — for a [Trickle](trickle.md) that means a clean re-bootstrap (a fresh main, an empty changelog) that raises the published *floor*, so downstream coverage-misses and reloads too.

Refresh is **lazy**: it changes *how* the next run computes, not *when*. Nothing runs the moment you flag it; the rebuild happens on the next genuine run, at a new freshness, so the correction propagates honestly through the graph. Use it after a logic fix that changes *history* (not just the latest slice), or to recover a Pond whose accumulated state is wrong — without un-deploying it. Often you set it and simply let the next end-to-end run heal everything.

## Repair — rebuild a set of Ponds now

```bash
duckstring control repair sales reports          # a connected set
duckstring control repair sales --downstream     # ...and everything below it
```

When you can't wait for a next run — the fix is urgent and no new upstream data is coming — **repair** rebuilds a chosen set immediately. It steps out of the demand model: the Catchment rebuilds each Pond in dependency order (each reads its freshly-rebuilt parents), holding the scope `repairing` (blocked from normal demand) until each one's turn.

The set must be **connected**: any two selected Ponds joined by a path must stay connected *through the selection* — selecting `A` and `D` but skipping the `B`/`C` between them is rejected (`D` would rebuild from stale parents). `--downstream` adds every descendant, which is always valid. The web UI offers the same as a click-to-select mode on the graph.

Repair is a one-off maintenance operation; for steady-state corrections, prefer plain [refresh](#refresh--rebuild-from-scratch-on-the-next-run) and let freshness carry it.

## Sleep — withdraw demand

```bash
duckstring control sleep reports
duckstring control sleep reports --upstream
```

Sleep clears all demand from the Pond — pull tokens and push targets — and cancels its standing trigger. It is gentle by design: a Pond Run already started completes normally; the Pond just won't start *new* work until demanded again.

`--upstream` extends the sleep to every ancestor, which is the clean way to quiet an entire lineage. Without it, upstream Ponds still holding their own demand keep running; only this Pond stops consuming.

Sleep is the right verb for maintenance windows and for pausing a noisy pipeline: nothing is lost, nothing is interrupted, and any trigger re-establishes the flow.

## Kill — stop now

```bash
duckstring control kill sales
```

Kill is the hard stop: the Pond's worker process is terminated mid-run, the running Pond Run is cancelled (recorded as `killed` in run history), and the Pond is parked in a **killed** state. Killed is terminal and deliberate — no retry budget applies, no Source update revives it, and everything downstream that requires this Pond is [blocked](fault-tolerance.md). It stays parked until an operator issues `wake`, `force`, or `clear`.

Use it on runaway work: a Ripple stuck on a hung connection, a transform chewing through resources, a run you know is producing garbage.

## Clear — reset without running

```bash
duckstring control clear sales
```

Clear resets a failed or killed Pond to idle *without running it*: the failure state is dropped, the halted run is abandoned (it won't be re-detected as a failure), and downstream Ponds unblock. Use it when the run shouldn't be repeated — the failure was transient, or you'd rather wait for the next natural trigger than force a recompute. Covered in depth in [Fault Tolerance](fault-tolerance.md#recovery).

## How the verbs relate

| | runs the Pond? | needs fresher Sources? | advances freshness? | clears failed/killed? |
|---|---|---|---|---|
| **Wake** | once, when input allows | yes (waits) | yes | yes |
| **Force** | immediately | no | no | yes |
| **Sleep** | no — stops new work | — | — | no |
| **Kill** | no — stops current work | — | — | no (causes killed) |
| **Clear** | no | — | — | yes |

In [demand-model](../concepts/freshness.md) terms: Wake is a one-shot pull that doesn't re-arm Sources; Force is a recompute at unchanged freshness; Sleep removes demand; Kill removes the worker. The same four verbs appear as the Control row in the [web UI](web-ui.md) sidebar.
