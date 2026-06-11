---
title: Triggers
description: Tap, Wave, Pulse, Tide — the demand signals.
---

# Triggers

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- The 2×2: push vs pull, once vs standing

  | | Once | Standing |
  |---|---|---|
  | **Push** | Pulse | Tide |
  | **Pull** | Tap | Wave |

- **Pulse** — push `now`, runs each Pond in the lineage once
- **Tide** — standing push with a *staleness bound* (`30s`, `1d`, … — not cron)
- **Tap** — one pull; re-arms upstream so the next Tap is supplied immediately
- **Wave** — standing pull; updates as frequently as the bottleneck allows
- `duckstring trigger remove` to drop a standing trigger (existing work drains)
- Triggers target any deployed Pond, not only Outlets
- The live status monitor each trigger opens (one-shots hang up on settle; standing stay open)
- Choosing a trigger: consumption pattern → trigger type
- Pointers: [Freshness & Demand](../concepts/freshness.md), [Windows](windows.md)
