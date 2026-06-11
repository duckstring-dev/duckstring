---
title: Ponds
description: The versioned package boundary — the unit of ownership, deployment, and dependency.
---

# Ponds

A **Pond** is a versioned Python package containing data transforms. It is the unit of everything organisational in Duckstring: one Pond has one owner, one version number, one deploy, and one declared set of dependencies. Everything inside a Pond is private; everything a Pond publishes is a versioned contract.

## A Pond is a package

A Pond project looks like a small Python package:

```text
sales/
├── src/
│   ├── pond.py      # the Ripples — the transform code
│   └── puddles.py   # Source snapshots for local testing
├── pond.toml        # name, version, type, Sources
├── .gitignore
└── README.md
```

The manifest carries its identity and its dependencies:

```toml
[pond]
name = "sales"
version = "1.0.0"

[sources]
transactions = "1.0.0"
products = "1.0.0"
```

That `[sources]` section is the entire pipeline definition, from this Pond's point of view. There is no global DAG file; the graph is the union of every Pond's declared Sources, exactly as a package index's dependency graph is the union of every package's requirements. See the [pond.toml reference](../reference/pond-toml.md) for every field.

## Kinds and relationships

Relative to one another, Ponds are **Sources** (parents) and **Sinks** (children). By position in the graph, a Pond is one of three kinds, declared as `type` in `pond.toml`:

- **Inlet** — no Sources. Inlets ingest from external systems (an API, a warehouse export, a file drop) and are where [Windows](../guides/windows.md) apply, since their availability is governed by the outside world.
- **Pond** — the default: transforms with both Sources and Sinks.
- **Outlet** — no Sinks. Outlets produce the final data products that applications and analysts consume, and are the natural place to attach [triggers](../guides/triggers.md).

## What's inside: Ripples

The executable content of a Pond is its [Ripples](ripples.md) — typically one per output table. When a Pond runs (a **Pond Run**), every Ripple in it runs, ordered by their declared intra-Pond dependencies. The Pond's boundary is what its Sinks see: a Sink never depends on an individual Ripple, only on the Pond and the tables it publishes.

## What a Pond publishes

Each successful run exports the Pond's tables as Parquet snapshots — the published, consistent output that Sinks and [queries](../guides/querying-data.md) read. A Sink reading `transactions.transaction` reads the last successfully exported snapshot, never a half-written intermediate state, and never contends with the Source's in-flight run.

## Why the package boundary matters

Because the Pond is a package, it inherits the package ecosystem's answers to coordination problems:

- **Ownership** — a team owns its Pond's repository and releases on its own schedule. Changing a transform never means editing shared orchestration code.
- **Versioning** — Ponds use SemVer, and a new major version runs *concurrently* with the old until every Sink has migrated. Breaking changes stop being organisation-wide events. See [Versioning](versioning.md).
- **Deployment** — deploys are atomic and per-Pond, like publishing a package. Deploy order doesn't matter; a Sink can even deploy before its Source exists. See [Deploying](../guides/deploying.md).
