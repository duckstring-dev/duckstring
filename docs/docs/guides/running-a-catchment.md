---
title: Running a Catchment
description: Start, connect to, and operate a Catchment.
---

# Running a Catchment

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- `duckstring catchment init --name dev --port 5000 --root ~/.duckstring/dev`; restarting with `duckstring catchment start`
- Connecting to a remote Catchment: `duckstring catchment connect --name … --path …`; selecting between Catchments with `-c`
- What lives in the catchment root on disk (database, per-Pond data and ledgers)
- Restart behaviour: state restores from disk; in-flight runs resume; workers survive Catchment downtime and reconnect
- `duckstring status` / `duckstring status {pond}` — the live monitor
- Environment variables and configuration
- Future: hosted Catchment service at duckstring.com
- Pointers: [The Catchment](../concepts/catchment.md), [Architecture](../reference/architecture.md)
