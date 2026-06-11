---
title: Freshness & Demand
description: The freshness-based Kanban that replaces schedules and DAG runs.
---

# Freshness & Demand

:::caution Work in progress
This page is a stub. Content to be written in a later pass. This is the approachable overview; the [Theory](../theory.md) page is the authoritative spec.
:::

**Planned content:**

- Freshness `F`: a timestamp per node — the run-start time of the oldest root feeding it; staleness = `now + D − F`
- **Pull** demand: run when a Source is fresher, re-arming Sources on start — demand propagates upstream
- **Push** demand: bring everything upstream of a target to a given freshness
- Why pull naturally throttles a pipeline to its bottleneck (no rate limits, no concurrency caps)
- When to use push vs pull (irregular vs continuous consumption)
- How the four triggers (Tap, Wave, Pulse, Tide) map onto pull/push, once/standing
- Completions clock the cascade: there is no cap on concurrent Pond Runs
- Pointers: [Triggers](../guides/triggers.md), [Windows](../guides/windows.md), [Theory](../theory.md)
