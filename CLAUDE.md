# Duckstring

A packaging standard for data transforms. Each transform is a versioned **Pond** (Python package) that declares its parent Ponds in `pond.toml`. The pipeline is implicit in the package graph. **Ripples** are the execution units within a Pond. The **Catchment** (FastAPI server) is the reference runtime — a convenience, not the product.

See `brand/strategy.md` for positioning rationale and `brand/copy.md` for settled copy.

Read `docs/guide/` before touching orchestration logic. `catchment.md`, `ponds.md`, and `ripples.md` are the design spec. **`orchestration.md` is outdated** — the authoritative rules are in the Orchestration section below.

## Brand & Positioning

- **Never describe Duckstring as an orchestration framework.** That positions it against Airflow/Prefect/Dagster. The differentiation is the package model, not the execution model.
- **The Catchment is not the product.** Don't lead with it in copy or docs introductions. It's the batteries-included runtime for teams that want the full stack.
- **Target audience**: data engineers who have hit the coordination and ownership walls of large transform pipelines — specifically those who've adopted or considered a mesh pattern and found that breaking changes still require organisation-wide coordination. They've reasoned their way to needing versioned package boundaries; they just don't have SemVer or concurrent version execution yet.
- **Tagline**: "There is no DAG." — the DAG exists but is implicit in the package graph. You don't build or govern it.
- **dbt Mesh users** are the warmest possible first audience. See `brand/strategy.md` for migration path and gaps.

## Structure

```
src/duckstring/
  core.py                    # Pond, Ripple, Trickle base classes (mostly stubs)
  catchment/
    app.py                   # FastAPI app
    db.py                    # SQLite connection + migration runner
    schema/001_init.sql      # Database schema
docs/guide/                  # Design documentation
frontend/                    # Next.js UI (built output served as FastAPI static)
```

## Catchment database

SQLite file at the catchment root. Schema in `catchment/schema/001_init.sql`, applied by `catchment/db.py:migrate()`. New migrations are numbered SQL files (`002_*.sql`, etc.). All file paths stored in the database are **relative to the catchment root**.

### Schema decisions

- `pond` (abstract named entity) / `pond_version` (specific deployed snapshot) — no `pond_major` table; the major version line is expressed as `(pond_id, major)` inline where needed.
- `is_active` on `pond_version` with a partial unique index: at most one active version per `(pond_id, major)`.
- `git_branch` on `pond` (abstract) — it's a configuration against the entity, not a version. `source_path` on `pond_version` — the materialised snapshot.
- `pond_to_pond` = inter-pond declared sources (from `pond.toml`). `ripple_to_ripple` = intra-pond parent edges only. These are strictly separate; never mix inter- and intra-pond edges in the same table.
- `watermark` is at Pond level `(sink_pond, source_pond, source_major)` — change monitoring only applies at the inter-Pond boundary, and watermarks must survive version upgrades within a major.
- `generation` belongs to `pond_run`, not `ripple`.
- `demand.sink_id` is null for trigger-sourced demand (no `pond_trigger_id` FK needed on `demand`).

**Note:** the schema and `pond_run`/`demand` tables are pending a refactor to match the new Ripple-level Kanban design described in the Orchestration section below. The above reflects current code, not the target design.

## Orchestration

**This design is not yet reflected in the code — a refactor of `demand`, `pond_run`, watermarks, and the orchestration logic is needed before implementation.**

The Pond is a versioned boundary around Ripples — it is the unit of packaging and version management. The Ripple is the execution unit. The Pond-level Kanban framework applies directly at the Ripple level; the Pond is just a boundary around its Ripples.

A **Pond run** is not an explicit record — it is the set of Ripple executions forming a connected chain, derivable from Ripple run history.

### Demand record (per Ripple, per sink)

`(sink_id, is_stop, is_persistent)` — upserted, one record per sink.

- `is_stop=false` — active demand
- `is_stop=true` — stop veto from this sink
- No record — idle/no relationship

Ripple state: **Active** (any `is_stop=false`) / **Stopped** (all `is_stop=true`) / **Idle** (no records).

### Starting conditions

A Ripple starts when ALL hold:

1. **Active** — or pulse-mode exception: a non-root Ripple whose parent(s) have updated with no downstream pull runs in **pulse mode** (no upstream demand propagation) to ensure initiated chains always complete.
2. **Source readiness:**
   - Non-root Ripples: all parent Ripples have `generation > watermark` (intra-pond parents are always required).
   - Root Ripples inherit the Pond's inter-pond source conditions: no sources → always ready; required sources → all must have `generation > watermark`; no required sources → at least one must have `generation > watermark`.
3. **Not currently running.**

### On start (before executing)

Propagate demand upstream before executing:
- Non-root, demand-driven: send demand to each parent Ripple.
- Root, demand-driven: upsert demand to each source Pond's leaf Ripples.
- **Type:** wave if any active demand is `is_persistent=true`; pulse if all pulse or cold-starting a stopped/idle source.
- Pulse-mode exception: no upstream propagation.

### Stop signals

**Eager:** the moment the last `is_stop=false` record flips, immediately propagate `is_stop=true` upstream — to parent Ripples (non-root) or source Pond leaf Ripples (root). Current run completes normally. All demand records (including stop records) cleared on run completion.

**Stop vs cancel:** stop = drain current run, propagate upstream, no new runs. Cancel = immediate halt (separate concept).

### On completion

1. Clear all demand records (including stop records).
2. Increment Ripple generation.
3. Advance watermarks.

Demand is cleared **before** generation increments — prevents new demand triggered by the increment from being spuriously cleared.

### On failure

Generation not incremented, demand not cleared, watermarks not advanced. Will not retry until parent Ripples (non-root) or source Pond leaf Ripples (root) produce a new generation.

### Cold start

A Ripple at generation 0 blocks all consumers regardless of required/non-required status. Demand propagates upstream until it produces its first generation.

### Inter-pond connections

A downstream Pond's dependency on an upstream Pond resolves to dependencies on the upstream Pond's **leaf Ripples**:
- Required → all leaf Ripples must have `generation > watermark`.
- Non-required → at least one leaf Ripple must have `generation > watermark`.

Demand arrives at leaf Ripples of the upstream Pond; demand is sent from root Ripples of the downstream Pond. Watermarks are tracked between these root/leaf Ripple pairs.

### Wave and pulse

- **Wave** (`is_persistent=true`): continuous updates; source keeps running as upstream produces new data.
- **Pulse** (`is_persistent=false`): one run; source idles after completion.
- Propagation: wave if any active demand is wave; pulse if all pulse.

## Before finishing any code change

Run `ruff check .` and fix all errors before considering the task done.

## Conventions

- Table names: **singular** (`pond`, not `ponds`).
- Association tables: **`{parent}_to_{child}`** for many-to-many; `{child}_in_{parent}` for nesting/hierarchy.
- FK columns: **`{table}_id`**; use a qualifier (`sink_id`, `source_id`) when two FKs reference the same table.
- Inter-pond and intra-pond concerns are kept in separate tables — do not unify them.
