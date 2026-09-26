---
title: Fault Tolerance
description: Retry budgets, failure states, and recovery.
---

# Fault Tolerance

Transforms fail — bad data, flaky connections, bugs. Duckstring's failure handling follows the same philosophy as its orchestration: failure is part of the freshness model, not a side-channel. A failed Pond is a Pond whose freshness stopped advancing; recovery is whatever makes it advance again. (The authoritative spec is the [Theory](../theory.md#fault-tolerance) Fault Tolerance section.)

## The two retry budgets

Every Pond carries two budgets, both defaulting to 0 (fail fast):

- **Immediate retries** — how many times a failed *Ripple* is re-attempted within the same Pond Run. Right for transient noise: a dropped connection, a momentary lock.
- **On-change retries** — how many *fresh Pond Runs* are attempted automatically as Sources update, after a run has given up. Right for failures that new data might fix — and a guard against burning compute when it won't.

Defaults can be declared in [`pond.toml`](../reference/pond-toml.md) (`immediate_retries`, `source_retries`), which seed the live values on the Pond's first deploy. From then on the budgets are **operator-owned** — inspected and edited live, surviving redeploys:

```bash
duckstring control failure-budget sales                          # show current budgets
duckstring control failure-budget sales --immediate 2 --on-change 1
```

## How a failure unfolds

Take a Ripple raising an exception mid-run:

1. The Ripple is retried up to the immediate budget, within the same run. Each attempt is recorded separately in run history.
2. The budget exhausts; the Pond Run fails. The run records the error and full traceback.
3. The Pond enters the **failed** state and stops accepting ordinary demand.
4. Downstream Ponds that *require* this Pond become **blocked**, transitively — visible at a glance rather than silently waiting.
5. If on-change budget remains, the next Source update triggers a fresh attempt automatically. A run that completes fresher than the failure clears the episode entirely — counters reset.
6. If the budget is spent, the Pond stays failed until [recovery](#recovery).

## Failure states

The per-Pond status reflects three distinct conditions (in this precedence, in both the CLI and [web UI](web-ui.md)):

- **failed** — a run gave up and hasn't been superseded. Every failure carries a message; Ripple and worker exceptions carry a full traceback.
- **killed** — an operator [killed](control.md) the Pond. Terminal by design: no retry budget applies until an operator acts.
- **blocked** — a *required* Source (anywhere upstream) is failed, killed, or itself blocked. Blocked is derived state, not a fault: the Pond is healthy but cannot be supplied.

The nuances: a blocked Pond still **drains** — if its Sources already hold fresher output, it consumes it; it just never solicits new work from a broken lineage. A Source declared optional (`name = "version?"` in `pond.toml`) never blocks its Sinks. And a failed Pond isn't dead — the on-change path stays open while budget remains.

## What counts as a failure

Beyond Ripple exceptions, the runtime detects:

- **Worker-level errors** — the Pond's worker process hits an internal error and reports it before exiting.
- **Dead workers** — the worker process disappears mid-run (OOM-killed, crashed). The Catchment notices the process is gone and fails the run.
- **Silent workers** — no contact for 60 seconds mid-run: failed on suspicion of a hang.
- **Stuck runs** — the worker is alive but has had outstanding work with no Ripple running for 30 seconds (e.g. an internal deadlock): the worker reports itself failed.

Every path produces a run-history row with a message; exception paths attach the traceback. Nothing fails silently, and nothing hangs forever.

What is *not* a failure: a [Catchment restart](running-a-catchment.md#restart-behaviour). Workers run independently through Catchment downtime, buffer their reports, and replay them on reconnect; interrupted Ponds resume from their ledger, re-running only incomplete Ripples.

## Recovery

Four paths out of failed/killed, by situation:

| Path | When |
|---|---|
| **Redeploy a fix** | The transform was wrong. Deploying the Pond [auto-clears](deploying.md) its failure — shipping the fix is the recovery. |
| `control force` | Want the recompute now, same inputs (e.g. after an environmental fix). Clears the state and re-runs immediately. |
| `control wake` | Want a re-run on current Source data. Clears the state and runs when input allows. |
| `control clear` | Don't want a run — just reset to idle and unblock downstream; the next natural demand takes it from there. |

In all cases downstream unblocks as soon as the Pond leaves the failed/killed state.

## Reading the trail

Run history keeps **one row per attempt**: a Ripple that failed twice and succeeded on the third try shows three rows under its Pond Run, each with status, timing, and any error. In the web UI's [Run Detail](web-ui.md) panel the retry trace appears as `↻N` markers, with each failure's message and full traceback below. From the API, `GET /api/runs?ripples=true` returns the same per-attempt records — see the [HTTP API](../reference/http-api.md).
