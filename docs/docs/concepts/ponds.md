---
title: Ponds
description: Versioning and Packaging of Transformations.
---

# Ponds

A **Pond** is a versioned set of data transformations. You can think of a Pond as a package with dependencies, much like software packages. It is the main organisational unit in Duckstring. 

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
change is made.

## Types

Ponds can have **Sources** (parents) and **Sinks** (children). By position in the graph, a Pond is one of three kinds, declared as `type` in `pond.toml`:

- **Inlet**: no Sources. Inlets ingest from external systems (an API, a warehouse export, a file drop).
- **Pond**: both Sources and Sinks.
- **Outlet**: no Sinks. Outlets produce the final data products that applications and analysts consume.

These are more guidelines than hard rules. However, if a Pond was built as an Outlet, typically it is worth remaking as a new Pond if a need
arises for another Pond to draw from it. Building for consumers typically comes with quite different design considerations than building for
a pipeline - mandating a Pond strictly as an Outlet avoids the inevitable spaghetti that comes with broadening scope beyond its initial purpose.

## Ripples

Where a Pond is the primary *organisational* unit, a [Ripple](ripples.md) is the primary *operational* unit, where Ripples belong to Ponds. These are similarly organised into a pipeline *within* the Pond. The same Ripple may not run simultaneously, but multiple instances of the same Pond may run concurrently. The main difference is that individual Ripples are not versioned, and dependencies are managed at the Pond level. 

Of course, there's nothing stopping you from using only one Ripple in a Pond - but the purpose of the separation is to split the concepts of
logical units (Ripples) from ownership, versioning and dependencies (Ponds).

## Versioning

Ponds are deployed to a [Catchment](catchments.md) by name. If a Pond of that name already exists, it's either:

- Upgraded if the existing Pond is within the same major version
- Added in parallel to the existing Pond if it is a new major version

The purpose of this is to allow seamless upgrades for non-breaking changes, and to retain the existing Pond for all its downstream dependencies
for breaking changes. Following these rules, you can confidently upload changes and upgrade downstream Ponds when possible - no need for strong
governance on simultaneous upgrades.

Each entry under `[sources]` pins both a major version and a minimum version: `transactions = "1.2.0"` means major version 1, at least 1.2.0.
A deployment that would break a pin is refused. A Sink can't be deployed against a Source older than its minimum, and a Source can't be rolled
back below a version that a deployed Sink requires.

Within a major version, a Pond's output may only grow. New tables, new columns and widening a column's type (such as `INTEGER` to `BIGINT`) are
fine. Removing a table or column, or narrowing a type, is a breaking change and needs a new major version. As a Pond's output is only known once
it runs, this is checked when a run publishes: a run whose output breaks the rule fails without publishing anything, and downstream Ponds keep
reading the last good output.

## Deployment and Execution

The *execution context* for Duckstring is the [Catchment](catchments.md). This manages execution, orchestration, data cataloging, querying
and cloud compute configuration. Because orchestration is set against the *downstream* Ponds (**Outlets**), uploaded Ponds are not executed
until something downstream depends on them.

## Data

Data is considered to be co-located with a Pond - objects exist adjacent to the logic generating them. A Pond's name and major version 
defines the *Schema* for its objects. A table `monthly_summary` in the `reports` Pond, under major version 2, may be queried by:

```
SELECT * FROM reports_v2.monthly_summary
```

Each Pond also has a *served* major version, which can be queried without the suffix. This is the first major version deployed, until you promote another one (`duckstring serve promote`), so consumers can be moved to a new major version in a single step:

```
SELECT * FROM reports.monthly_summary
```

It is however safer to be explicit, so it's recommended to always include the major version.

## Puddles

Developing and testing against the entire live dataset is generally slow and wasteful. A Puddle defines queries against the Catchment
for generating a sample snapshot of every Source object used in the Pond. Transformations can then be executed against this locally
for testing.
