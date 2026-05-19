# Ripples

A **Ripple** is the unit operation inside a Pond — the smallest thing that can be invoked, depended on, and write a table. A Pond is a versioned container; the Ripples inside it are what actually do the work.

Each Ripple has a name that is unique within its Pond. That name is what users reference externally: in `duckstring get dev outlet daily`, `outlet` is the Pond and `daily` is a Ripple inside it.

A Pond will often have just one Ripple, in which case the Ripple and the Pond are effectively coextensive. But splitting a Pond into multiple Ripples is encouraged whenever there is internal structure worth expressing — see "Parents and ordering" below.

## Two forms, one runtime type

Ripples can be written in two styles. Both produce the same runtime object — the decorator form is sugar that constructs a `Ripple` instance whose `run` method calls the decorated function.

### Function form

The frictionless default. A plain function decorated with `@ripple`:

```python
from duckstring import ripple

@ripple
def load(pond):
    pond.write_table("raw", pond.read_table("inlet.daily"))

@ripple(parents=[load])
def clean(pond):
    df = pond.con.sql("SELECT * FROM raw WHERE value IS NOT NULL")
    pond.write_table("clean", df)
```

The function name becomes the Ripple name. To override:

```python
@ripple(name="daily_clean", parents=[load])
def _clean(pond):
    ...
```

### Class form

For stateful work, shared helpers, or as the base for a `Trickle`. Subclass `Ripple` and implement `run`:

```python
from duckstring import Ripple

class Clean(Ripple):
    parents = [Load]

    def run(self, pond):
        df = pond.con.sql("SELECT * FROM raw WHERE value IS NOT NULL")
        pond.write_table("clean", df)
```

The class name becomes the Ripple name (snake-cased: `Clean` → `clean`). Override with a `name` class attribute if needed.

The two forms are interchangeable from the framework's point of view — choose whichever fits the Ripple's complexity.

## The `pond` handle

Every Ripple receives a single argument, conventionally named `pond`. This is an instance of the `Pond` runtime type — the same noun as `pond.toml`. The Pond is the persistent unit, the thing under version control. The Catchment is just the compute that activates a Pond for a run, much like attaching a kernel to a notebook. Naming the handle `pond` keeps that framing visible in every Ripple body.

The handle's surface:

| Attribute / method | Purpose |
| --- | --- |
| `pond.path` | The Catchment-allocated working directory for this Ripple's outputs. |
| `pond.write_table(name, data, *, mode="replace")` | Persist and register a table. `data` accepts anything DuckDB/Ibis/Arrow can ingest (relation, DataFrame, Arrow table, parquet path). |
| `pond.read_table(ref)` | Read a table produced by an upstream Ripple in this Pond, or by a declared Source Pond. See "Reading tables" below. |
| `pond.con` | A DuckDB connection scoped to this run, with Source-Pond tables pre-attached read-only. |
| `pond.log` | A run-scoped logger. |
| `pond.run` | Run metadata — generation number, triggering Sink, demand id, etc. |

The Catchment constructs the handle, picks the working directory, opens the connection, attaches Source tables, and hands it in. The Ripple does not touch any of this directly.

Function form: `def name(pond): ...`. Class form: `def run(self, pond): ...`.

## Parents and ordering

A Ripple declares its **parents** — the other Ripples in the same Pond it depends on — at definition time:

```python
@ripple(parents=[load, validate])
def enrich(pond):
    ...
```

```python
class Enrich(Ripple):
    parents = [Load, Validate]
    ...
```

Parents are declared **by reference**, not by string name. That keeps static analysis honest (a typo is a `NameError`, not a runtime DAG failure) and means renaming a Ripple updates its references with normal refactor tools.

A few rules:

- **Siblings run in parallel.** Ripples with no dependency relationship may execute concurrently.
- **The Catchment introspects the DAG before execution.** Cycles are rejected at registration time, not at run time.
- **Roots are Inlet-equivalents.** A Ripple with no parents is the entry point for its part of the Pond's DAG.
- **Leaves are Outlet-equivalents.** A Ripple with no children is what downstream Ponds consume from.
- **Cross-Pond data flows through `pond.read_table`** against tables declared by Ripples in Source Ponds. Intra-Pond ordering uses `parents=`; cross-Pond ordering uses `pond.toml`'s `[sources]` section.

The intra-Pond DAG mirrors the inter-Pond DAG described in `orchestration.md`. Demand arriving at the Pond reaches its leaf Ripples; demand leaving the Pond comes from its root Ripples.

## Writing tables

`pond.write_table` is the only way a Ripple emits data the framework knows about. Anything written directly into `pond.path` (raw files, logs, scratch artifacts) is reachable via `duckstring get` but **not** via `duckstring query`. Tables are addressed externally as `pond.ripple.table` and registered with the Catchment at write time.

A Ripple may write multiple tables:

```python
@ripple(parents=[load])
def split(pond):
    df = pond.con.sql("SELECT * FROM raw")
    pond.write_table("valid",   df.filter("value IS NOT NULL"))
    pond.write_table("invalid", df.filter("value IS NULL"))
```

The schema is captured on the first successful write. A subsequent write of the same table name within the same Ripple replaces it (default `mode="replace"`); other modes are reserved for `Trickle`.

## Reading tables

`pond.read_table(ref)` resolves in this order:

1. **Bare name** (`"raw"`) — searches this Pond's own Ripples first, then declared Source Ponds.
2. **Qualified by Pond** (`"inlet.daily"`) — reads `daily` from the Source Pond `inlet`.
3. **Fully qualified** (`"inlet.load.daily"`) — disambiguates when a Source Pond has multiple Ripples producing tables of the same name.

Resolution is performed by the Catchment using its table registry; see `docs/guide/catchment.md`.

## Lifecycle inside a Pond run

When the Catchment runs the Pond:

1. **Discover** — `src/pond.py` is imported; every `@ripple`-decorated function and every `Ripple` subclass at module scope is collected.
2. **Build** — the intra-Pond DAG is constructed from each Ripple's `parents`. Cycles or duplicate names are rejected here.
3. **Execute** — root Ripples start first. Siblings run in parallel. Each Ripple's `run` is called with a freshly constructed `pond` handle.
4. **Register** — each `pond.write_table` call writes to `pond.path` and registers the table with the Catchment's table registry in the same call.
5. **Propagate** — downstream Ripples see the new tables via `pond.read_table`. Downstream Ponds see them via their own `pond.read_table` against this Pond's name.

## Trickle (placeholder)

A **Trickle** is a `Ripple` subclass for incremental tabular work. It exchanges generality for strong guarantees about how state advances between runs, so the Catchment can support resume, backfill, and watermark-aware execution.

The contract (sketched; full API in a later spec pass):

- **Tabular only.** A Trickle produces exactly one table with a stable schema.
- **All parents must be Trickles.** Incremental work cannot consume from a batch Ripple — the upstream watermarks have to exist for the downstream watermark to mean anything.
- **Declared watermark.** Either a watermark column (e.g. an `updated_at` timestamp) or a composite key. The Catchment uses this to resume after the last successful generation.
- **Append/merge by default.** `write_table` semantics shift from `replace` to merge-on-key — a Trickle accumulates state across generations rather than overwriting it.

A Trickle in user code will look like a `Ripple` subclass with extra class attributes for the watermark and merge key. The exact API surface — declaration syntax, replay semantics, schema-evolution policy — is deferred until the batch path is solid.
