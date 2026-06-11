---
title: Quickstart
description: From zero to a running pipeline with the demo Ponds.
---

# Quickstart

:::caution Work in progress
This page is a stub. Content to be written in a later pass. The current README quickstart is the source material.
:::

**Planned content:**

- Start a local Catchment: `duckstring catchment init --name dev --port 5000 --root ~/.duckstring/dev`; restart with `duckstring catchment start dev`
- Generate the demo Ponds (`duckstring pond demo`): products + transactions → sales → reports, with the mermaid topology diagram
- Deploy: `duckstring pond deploy` / `duckstring pond deploy --all`
- Trigger a first run: `duckstring trigger pulse reports`, watch the live status monitor
- Keep it flowing: `duckstring trigger wave reports` and the bottleneck-throttled cadence
- Look at the results: `duckstring query reports --table monthly_summary`
- Open the web UI in the browser
- Pointers onward: [Triggers](../guides/triggers.md), [Creating a Pond](../guides/creating-a-pond.md), [Theory](../theory.md)
