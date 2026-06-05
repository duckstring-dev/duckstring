# Duckstring

A packaging standard for data transforms. Each transform is a versioned **Pond** (Python package) that declares its parent Ponds in `pond.toml`. The pipeline is implicit in the package graph. **Ripples** are the execution units within a Pond. The **Catchment** (FastAPI server) is the reference runtime — a convenience, not the product.

See `brand/strategy.md` for positioning rationale and `brand/copy.md` for settled copy.

Read `docs/guide/` before touching orchestration logic. `catchment.md`, `ponds.md`, and `ripples.md` are the design spec. **`docs/guide/theory.md` is the authoritative orchestration spec** — its "Pond State Variables" pseudocode is the exact state machine. The reference implementation is the playground engine at `frontend/src/lib/orchestration.ts` (a well-tested TypeScript simulation; the Python engine should be a faithful port). **`orchestration.md` and the Orchestration section below are outdated** — superseded by theory.md.

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

**Note:** the schema reflects current code, not the target design. The `watermark`, `generation`, `pond_run`, and `demand` tables encode the *superseded* generation/watermark model (see Orchestration below) and need a redesign to the freshness/push-token model in `theory.md`. The versioning tables (`pond`, `pond_version`, `pond_to_pond`, `ripple_to_ripple`) are unaffected.

## Orchestration

**Authoritative spec: `docs/guide/theory.md`** (the "Pond State Variables" pseudocode is the exact state machine). **Reference implementation: `frontend/src/lib/orchestration.ts`** — a well-tested TypeScript simulation; the Python engine should be a faithful port.

Freshness-based Kanban at the Ripple level. The Pond is a packaging/versioning boundary; its `start`/`end` are zero-duration boundary nodes carried as Pond state, so Pond and Ripple share the same rules.

- **Freshness `F`** — a timestamp per node: the run-start time of the oldest root feeding it (with windows, the "fresh until" window end). Staleness = `now + D - F`. This replaces generations.
- **Pull** (Tap/Wave) — a `hasPull` token. A node runs when a parent is fresher than it (`sourceF > startF`) and re-arms its parents on start; cold-start demand propagates up to idle/behind ancestors.
- **Push** (Pulse/Tide) — a **set** of unsatisfied target freshnesses per node (not a single value). A node runs when `sourceF >= min(targets)`, taking the freshest input and clearing every target it reaches; targets propagate eagerly to required parents.
- **Triggers** — Tap (one pull), Wave (re-pull on completion/idle), Pulse (one push to `now`), Tide (a clock: push `now` whenever the last requested freshness ages past the staleness bound).
- **Required vs optional** parents/sources, and **Windows** on Inlets (batch sources), behave as in theory.md.

**Superseded — do not build:** the generation-counter + per-edge watermark + `demand(sink_id, is_stop, is_persistent)` design that previously filled this section. The freshness/push-token model replaces all of it.

## Before finishing any code change

Run `ruff check .` and fix all errors before considering the task done.

## Conventions

- Table names: **singular** (`pond`, not `ponds`).
- Association tables: **`{parent}_to_{child}`** for many-to-many; `{child}_in_{parent}` for nesting/hierarchy.
- FK columns: **`{table}_id`**; use a qualifier (`sink_id`, `source_id`) when two FKs reference the same table.
- Inter-pond and intra-pond concerns are kept in separate tables — do not unify them.
