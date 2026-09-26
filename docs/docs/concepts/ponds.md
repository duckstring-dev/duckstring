---
title: Ponds
description: The versioned package boundary — the unit of ownership, deployment, and dependency.
---

# Ponds

A **Pond** is a versioned set of data transformations. It is the main organisational unit in Duckstring: one Pond has one owner, one version number, one deploy, and one declared set of dependencies. You can think of a Pond as a package with dependencies, much like software packages.

## Structure

A Pond project looks like a small Python package:

```text
sales/
├── src/
│   ├── pond.py      # Ripples containing the transform code
│   └── puddles.py   # Source snapshots for local testing
├── pond.toml        # name, version, type, Sources
├── .gitignore
└── README.md
```

`pond.toml` specifies a Pond's identity and its dependencies.

```toml
[pond]
name = "sales"
version = "1.0.0"

[sources]
transactions = "1.0.0"
products = "1.0.0"
```

Nowhere is the pipeline of Ponds specified outside of this list of dependencies. Declaring sources allows the Pond to consume from
the listed parent Ponds provided they match their major version. That allows upgrades to be made upstream until a major (breaking)
change is made. See the [pond.toml reference](../reference/pond-toml.md) for every field.

## Types

Ponds can have **Sources** (parents) and **Sinks** (children). By position in the graph, a Pond is one of three kinds, declared as `type` in `pond.toml`:

- **Inlet** — no Sources. Inlets ingest from external systems (an API, a warehouse export, a file drop).
- **Pond** — both Sources and Sinks.
- **Outlet** — no Sinks. Outlets produce the final data products that applications and analysts consume.

These are more guidelines than hard rules. However, if a Pond was built as an Outlet, typically it is worth remaking as a new Pond if a need
arises for another Pond to draw from it. Building for consumers typically comes with quite different design considerations than building for
a pipeline - mandating a Pond strictly as an Outlet avoids the inevitable spaghetti that comes with broadening scope beyond its initial purpose.

## Ripples

Where a Pond is the primary *organisational* unit, a [Ripple](ripples.md) is the primary *operational* unit, where Ripples belong to Ponds. 

The executable content of a Pond is its  — typically one per output table. When a Pond runs (a **Pond Run**), every Ripple in it runs, ordered by their declared intra-Pond dependencies. The Pond's boundary is what its Sinks see: a Sink never depends on an individual Ripple, only on the Pond and the tables it publishes.

## Purpose



Because the Pond is a package, it inherits the package ecosystem's answers to coordination problems:

- **Ownership** — a team owns its Pond's repository and releases on its own schedule. Changing a transform never means editing shared orchestration code.
- **Versioning** — Ponds use SemVer, and a new major version runs *concurrently* with the old until every Sink has migrated. Breaking changes stop being organisation-wide events. See [Versioning](versioning.md).
- **Deployment** — deploys are atomic and per-Pond, like publishing a package. Deploy order doesn't matter; a Sink can even deploy before its Source exists. See [Deploying](../guides/deploying.md).
