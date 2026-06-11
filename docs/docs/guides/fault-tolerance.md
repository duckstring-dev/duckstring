---
title: Fault Tolerance
description: Retry budgets, failure states, and recovery.
---

# Fault Tolerance

:::caution Work in progress
This page is a stub. Content to be written in a later pass. The Theory page's Fault Tolerance section is the authoritative spec.
:::

**Planned content:**

- The two retry budgets (default 0): **immediate** (retries within one Pond Run) and **on-change** (whole-Run re-attempts when a Source updates)
- Setting budgets: `pond.toml` defaults on deploy, then live via `duckstring control failure-budget --immediate N --on-change N`
- Failure states and their precedence: **failed → killed → blocked**; blocked propagates downstream
- What a blocked Pond still does (drains existing Source output) and doesn't (never solicits)
- Failure sources: Ripple errors, worker-level errors, dead/silent workers, stuck runs, operator Kill — all surface a message (and traceback where applicable)
- Recovery paths: on-change retry, redeploying a fix (auto-clear), `control clear`/`force`/`wake`
- Reading the retry trace in run history (one row per attempt) and the failure detail in the UI
- Worker resilience: in-flight runs survive Catchment downtime; events replay idempotently
- Pointers: [Control](control.md), [Theory](../theory.md)
