---
title: Ponds
description: The versioned package boundary — the unit of ownership, deployment, and dependency.
---

# Ponds

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- A Pond is a versioned Python package containing data transforms; `pond.toml` declares its name, version, kind, and parent Ponds
- Pond kinds: **Inlet** (no Sources, external dependencies), **Pond**, **Outlet** (no Sinks)
- Relational vocabulary: **Source** (a parent Pond), **Sink** (a child Pond)
- The package graph *is* the pipeline — dependencies are declared per Pond, never assembled centrally
- Project layout produced by `duckstring pond init` (`src/`, `pond.toml`, `__main__.py`)
- One Pond, one owner: the ownership and coordination story
- How a Pond executes: its Ripples, run boundaries (`start`/`end`), and the per-Pond data outputs (Parquet exports)
- Pointers: [Ripples](ripples.md), [Versioning](versioning.md), [Creating a Pond](../guides/creating-a-pond.md)
