---
title: Web UI
description: The Catchment's built-in monitoring and control surface.
---

# Web UI

:::caution Work in progress
This page is a stub. Content to be written in a later pass. Screenshots to be captured once copy is settled.
:::

**Planned content:**

- What it is: a read-mostly UI served by the Catchment at `/` — live topology, run history, and the trigger/control surface; topology itself is read-only (Ponds come from deploying code)
- The DAG canvas: Pond/Ripple nodes, state colours (running/queued/idle, failed/killed/blocked), pull vs push edges
- The sidebar: per-Pond freshness and staleness, the Trigger row, the Control row (Force/Wake/Sleep/Kill), failure budgets and Clear Failure
- Run history + run detail: per-attempt Ripple lists, retry traces, errors with full tracebacks
- Windows editor
- Querying exported data from the UI
- Pointers: [Triggers](triggers.md), [Control](control.md), [Fault tolerance](fault-tolerance.md)
