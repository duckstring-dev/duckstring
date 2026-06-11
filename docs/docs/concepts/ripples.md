---
title: Ripples
description: The execution units within a Pond.
---

# Ripples

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- A Ripple is a single unit operation within a Pond — typically one transformation producing one table
- The `@ripple` decorator and the Pond/Ripple runtime handles (`duckstring.core`)
- Intra-Pond dependencies between Ripples (all required) and how they form the Pond's internal graph
- Ripples and freshness: every Ripple is pushed to the Pond Run's frontier; the bottleneck Ripple sets the pipeline cadence
- Reading from Sources and writing tables (DuckDB registry + Parquet export)
- Pointers: [Ponds](ponds.md), [Freshness](freshness.md), [Theory](../theory.md)
