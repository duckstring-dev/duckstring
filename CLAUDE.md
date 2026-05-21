# Duckstring

A data pipeline orchestration framework built around versioned **Ponds** (Python packages) executing in a **Catchment** (FastAPI server). Ponds declare source dependencies in `pond.toml`; **Ripples** are the execution units within a Pond.

Read `docs/guide/` before touching orchestration logic. `catchment.md`, `orchestration.md`, `ponds.md`, and `ripples.md` are the design spec.

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

## Orchestration

Orchestration runs at the **Ripple level**. The Pond is the organisational/versioning unit; the Ripple is the execution unit. See `docs/guide/orchestration.md` for the full execution conditions.

Key points not obvious from a quick read:

- Intra-pond uses **pull-based demand** (same mechanism as inter-pond, not push). This enables pipelining: B2 writes demand to B1 *before* executing, so B1 starts its next generation in parallel with B2's current run. The bottleneck is the slowest single Ripple, not the sum of a Pond's Ripples.
- Watermarks are **Pond-level**, not Ripple-level. Intra-pond Ripples always execute together within a `pond_run`; no intra-pond watermarks are needed.
- A failed run does **not** retry until a source Pond produces a new generation — prevents infinite loops.

### Trigger Process

Orchestration executes at the **Ripple level** — the Pond is the organisational and versioning unit, but the Ripple is the execution unit. The process below applies to each Ripple individually.

A Ripple begins execution when all of the following hold:

1. **It has Demand from at least one Sink.** A Sink is any downstream Ripple (whether in the same Pond or a downstream Pond) that has written a Demand record targeting this Ripple.
2. **Source readiness is satisfied.** One of:
    - No inter-Pond sources (i.e. this is a root Ripple with no upstream Ponds): unconditionally ready.
    - Has inter-Pond sources, none marked required: at least one source Pond has *unconsumed changes* — its current generation exceeds the watermark this Pond holds for it.
    - Has one or more required inter-Pond sources: *all* required source Ponds have unconsumed changes.
3. **No run is currently in progress** for this Ripple.

"Unconsumed changes" means the source Pond's latest `pond_run.generation` is strictly greater than the generation recorded in the consumer Pond's `watermark` row for that source.

**On execution:** Demand is sent to source Ripples *before* the Ripple begins executing. This allows sources to start their next generation in parallel with the current execution, minimising end-to-end latency through the chain.

**Demand during a run** is held. On completion, if Demand remains and source readiness is still satisfied, execution begins immediately.

**On success:** Generation increments, watermarks advance, and all Demand for this Ripple is cleared (regardless of how many distinct Sinks wrote it).

**On failure:** Generation is not incremented, watermarks are not advanced, and Demand is not cleared. The Ripple will not retry until at least one source Pond has produced a new generation — this prevents infinite retry loops while allowing natural recovery when upstream data is refreshed.

**Cold start:** A source Pond at generation 0 (never run) is treated as not yet having produced output. Demand propagates upstream toward it so it runs before the consumer can satisfy its readiness check.

## Conventions

- Table names: **singular** (`pond`, not `ponds`).
- Association tables: **`{parent}_to_{child}`** for many-to-many; `{child}_in_{parent}` for nesting/hierarchy.
- FK columns: **`{table}_id`**; use a qualifier (`sink_id`, `source_id`) when two FKs reference the same table.
- Inter-pond and intra-pond concerns are kept in separate tables — do not unify them.
