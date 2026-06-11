---
title: Querying Data
description: Retrieve Pond outputs — files, SQL, and exports.
---

# Querying Data

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- `duckstring get {pond} {ripple}` — fetch a Ripple's output directory (`--path` to override the destination)
- `duckstring query {pond} --sql "…"` / `--sql @file.sql` / `--table {name}` for a quick glimpse
- Output formats: `--csv`, `--json`, `--parquet`, with `--path`
- Where data lives: each Pond exports `ponds/{pond}/data/{table}.parquet`; queries read the exports, never the live registry, so they don't contend with running work
- Consuming Pond outputs from external applications (and pairing reads with a Tap)
- Pointers: [Ripples](../concepts/ripples.md), [HTTP API](../reference/http-api.md)
