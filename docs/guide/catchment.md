# Catchment

A **Catchment** is the long-running process that hosts your Ponds. The same `duckstring catchment` command runs at every scale: on a laptop during development, on a shared VM or container when more developers join, and (eventually) hosted at `duckstring.com`. The artifact moves; the interface doesn't.

Keeping it as a daemon — rather than a script invoked per task — is what makes Wave, Tide, Demand/Stop, and the live UI possible: each needs somewhere with a heartbeat. For cron-style workflows, a Catchment can still be started, run once via Pulse, and shut down; the daemon path is just the headline.

The Catchment owns:

- **Storage** — deployed Pond sources, per-run working directories, the table registry, and Catchment-level state.
- **The Pond registry** — which Ponds have been deployed, in which versions, and their declared Sources.
- **The table registry** — which `pond.ripple.table` names exist, where they live on disk, and their schemas.
- **Orchestration state** — outstanding Demand, Stops, schedules, and run status. The mechanics are covered in `docs/guide/orchestration.md`.
- **The HTTP / UI surface** — what the CLI and the web UI talk to.

The framing matters: a **Pond is the persistent unit** — it lives in a git repo, has a `pond.toml`, has version history, and has a usage contract with downstream Ponds. A **Catchment is compute** — it activates a Pond for a run, supplies it with paths and a connection, collects its outputs, and goes idle. The same Pond can run in many Catchments (`dev`, `qa`, `prod`); the same Catchment hosts many Ponds. Think of attaching a kernel to a notebook: the notebook is the artifact, the kernel is the runtime.

## Storage layout

Everything a Catchment owns lives under its root directory (default `~/.duckstring/{name}/`, overridable with `--root` on `duckstring catchment dev`).

```
~/.duckstring/dev/
|-- catchment.toml          # Catchment-level config and state
|-- ponds/
|   |-- inlet/
|   |   |-- 1.0.0/          # Deployed Pond source (per name + version)
|   |   |-- 1.1.0/
|   |   |-- 2.0.0/
|   |-- pond/
|   |   |-- 1.0.0/
|   |-- outlet/
|       |-- 1.0.0/
|-- runs/
|   |-- {run-id}/
|       |-- {ripple-name}/  # Working directory handed to a Ripple as pond.path
|-- tables/
|   |-- inlet@1.0.0/
|   |   |-- load/
|   |       |-- daily.parquet
|   |-- pond@1.0.0/
|       |-- clean/
|           |-- clean.parquet
|-- registry.duckdb         # Table registry: name -> path, schema, run, ...
```

- **`ponds/{name}/{version}/`** — the deployed source tree for each Pond version. Multiple major versions can coexist (see SemVer rules in `ponds.md`).
- **`runs/{run-id}/{ripple-name}/`** — the working directory the Catchment hands to each Ripple as `pond.path`. Anything a Ripple writes here that isn't a `write_table` call is reachable via `duckstring get` but not `duckstring query`.
- **`tables/{pond}@{version}/{ripple}/{table}.parquet`** — durable table outputs, written by `pond.write_table`.
- **`registry.duckdb`** — the table registry. Resolves `pond.ripple.table` references to on-disk paths.

## The runtime contract with a Ripple

For each Pond run, the Catchment instantiates a `Pond` runtime handle — an instance of the `Pond` class — and passes it to every Ripple in that run. This is the entire surface a Ripple sees of the framework.

What that handle exposes and how each piece is wired by the Catchment:

- **`pond.path`** — the Catchment-allocated output directory for the current Ripple, under `runs/{run-id}/{ripple-name}/`.
- **`pond.con`** — a per-run DuckDB connection. The Catchment pre-attaches the Pond's declared Source tables read-only so the Ripple can query them without setup.
- **`pond.write_table(name, data, *, mode="replace")`** — writes under `pond.path`, copies (or moves) the result into `tables/{pond}@{version}/{ripple}/`, and registers it in `registry.duckdb` in the same call. Subsequent reads — whether intra-Pond, cross-Pond, or via `duckstring query` — go through the registry.
- **`pond.read_table(ref)`** — resolution order:
  1. Bare name (`"raw"`) — this Pond's Ripples first, then declared Sources.
  2. Pond-qualified (`"inlet.daily"`) — the table `daily` produced by some Ripple in Source Pond `inlet`.
  3. Fully qualified (`"inlet.load.daily"`) — disambiguates when several Ripples in a Source Pond produce tables of the same name.
- **`pond.log`** — a logger whose output the Catchment captures into per-run logs.
- **`pond.run`** — run metadata (generation number, triggering Sink, demand id) for Ripples that need to make decisions based on the surrounding run.

The Ripple never opens its own connection, picks its own paths, or talks to the registry directly. The handle is the contract.

## Discovering a Pond

When a Pond is deployed (or before it runs), the Catchment loads its `src/pond.py` in a subprocess. Every `@ripple`-decorated function and every `Ripple` subclass at module scope is collected; their declared `parents` form the intra-Pond DAG. The Catchment validates the DAG (no cycles, no duplicate Ripple names) and stores the structure alongside the Pond's source.

For larger Ponds whose Ripples live in other modules under `src/`, the convention is to import those modules from `src/pond.py` so registration happens at load time. The file is the registration surface; whatever is imported into it is what the Catchment sees.

## Deployment

A Pond reaches a Catchment one of two ways:

- **Local** — `duckstring deploy dev` from the Pond's project root uploads the working tree. Convenient for development; not reproducible across machines.
- **Git** — `duckstring deploy dev --git {branch|commit|tag}` registers the Pond by repository reference. On each execution the Catchment clones the ref and runs from a clean checkout. Reproducible and the recommended mode for `qa` / `prod` Catchments.

Versions resolve at run time using the SemVer rules in `docs/guide/ponds.md`: a Pond accepts any version greater-or-equal to its declared minimum within the same major. Multiple major versions of the same Pond may run concurrently in the same Catchment.

## HTTP / UI surface

The CLI is a thin client over the Catchment's HTTP API. The endpoints group as:

- **Deploy** — upload a local Pond, or register a git-tracked one.
- **Demand** — `pulse`, `wave`, `tide` against a named Outlet; `stop` to clear it.
- **Status** — current Demand, currently executing Ponds, recent runs.
- **Data** — `get` (raw directory contents) and `query` (SQL against the table registry).

The web UI uses the same endpoints. The full API spec is deferred — this section is just an outline of what's expected to exist.

## What's out of scope (for now)

- **Multi-node Catchments.** A Catchment is currently one process on one host. Scaling beyond a single machine is a non-goal until the single-node story is solid.
- **Engines other than DuckDB.** The engine is configurable in principle, but only the DuckDB path will be implemented first.
- **Auth.** The Catchment is intended for trusted environments. Authentication and authorization are future work.
