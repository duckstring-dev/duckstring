---
title: The Catchment
description: The reference runtime — a convenience, not the product.
---

# The Catchment

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- What the Catchment is: the batteries-included reference runtime (FastAPI server + web UI) that receives deployed Ponds and decides runs
- What it is *not*: Duckstring is the packaging standard; the Catchment is one runtime for it
- Local daemon vs remote server; connecting with `duckstring catchment connect`; the `-c` flag for multiple Catchments
- The two-tier runtime at a glance: the Catchment owns pull and decides Pond Runs; each executing Pond runs a **Duck** worker process
- Catchment state: triggers, windows, run history, the catchment root on disk
- Pointers: [Running a Catchment](../guides/running-a-catchment.md), [Architecture](../reference/architecture.md)
