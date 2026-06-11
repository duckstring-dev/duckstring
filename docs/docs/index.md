---
title: Introduction
description: Duckstring treats data transformations as software packages. There is no DAG.
slug: /
---

# Duckstring

*There is no DAG.*

Duckstring is a packaging standard for data transforms. Each transform is a versioned **Pond** — a Python package that declares its upstream dependencies in `pond.toml`. The pipeline is implicit in the package graph: you never build, draw, or govern a DAG.

Ponds are upgraded and deployed atomically, like upgrading a package, with earlier versions continuing to execute until no consumer depends on them. Upstream declares constraints on what may be consumed; downstream declares when it is needed; the runtime executes the sequence of Ponds in between with the best currency and frequency possible.

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

<!-- TODO: write this page. Planned content: -->

**Planned content:**

- The coordination and ownership problem in large transform pipelines (the walls mesh users hit)
- The package model: versioned boundaries, SemVer, concurrent version execution
- "There is no DAG" — the DAG exists but is implicit in the package graph
- Core vocabulary at a glance: Pond, Ripple, Catchment, Source/Sink, Inlet/Outlet
- Where to go next: [Quickstart](getting-started/quickstart.md), [Concepts](concepts/ponds.md), [Theory](theory.md)
- Link to the [Playground](https://playground.duckstring.com) for a zero-install feel of the orchestration

:::note Positioning
Per `brand/strategy.md`: never describe Duckstring as an orchestration framework, and don't lead with the Catchment — it is the batteries-included runtime, not the product.
:::
