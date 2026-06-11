---
title: Windows
description: Batch availability on Inlets — when external data is actually fresh.
---

# Windows

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- What a Window is: an allowed start period for an Inlet, with data "fresh until" the window's end
- RFC-5545-flavoured recurrence (no cron): `--every`, `--duration` (defaults to back-to-back), `--start`, `--until`, `--on` weekdays
- CLI: `duckstring trigger window {pond} add|list|remove`
- Windows are operational config (CLI/API, survives redeploys) — not declared in `pond.toml`
- Window + Wave: run a daily-updating source exactly once a day, at the right time, without a schedule
- Modelling "do not consume" periods (e.g. during upstream writes)
- Overlap validation on add
- Pointers: [Triggers](triggers.md), [Theory](../theory.md)
