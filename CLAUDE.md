# Duckstring

A packaging standard for data transforms. Each transform is a versioned **Pond** (Python package) that declares its parent Ponds in `pond.toml`. The pipeline is implicit in the package graph. **Ripples** are the execution units within a Pond. The **Catchment** (FastAPI server) is the reference runtime — a convenience, not the product.

See `brand/strategy.md` for positioning rationale and `brand/copy.md` for settled copy.

`docs/guide/theory.md` is the **authoritative orchestration spec** — its "Pond State Variables" pseudocode is the exact state machine. `playground/src/lib/orchestration.ts` is a well-tested TypeScript *simulation* (the standalone playground); the Python engine is a faithful, behaviour-for-behaviour port of it. The other `docs/guide/` files (`catchment.md`, `ponds.md`, `ripples.md`) are design background; `docs/guide/orchestration.md` is outdated (superseded by theory.md).

## Brand & Positioning

- **Never describe Duckstring as an orchestration framework.** That positions it against Airflow/Prefect/Dagster. The differentiation is the package model, not the execution model.
- **The Catchment is not the product.** Don't lead with it in copy or docs introductions. It's the batteries-included runtime for teams that want the full stack.
- **Target audience**: data engineers who have hit the coordination and ownership walls of large transform pipelines — specifically those who've adopted or considered a mesh pattern and found that breaking changes still require organisation-wide coordination. They've reasoned their way to needing versioned package boundaries; they just don't have SemVer or concurrent version execution yet.
- **Tagline**: "There is no DAG." — the DAG exists but is implicit in the package graph. You don't build or govern it.
- **dbt Mesh users** are the warmest possible first audience. See `brand/strategy.md` for migration path and gaps.

## Current state (2026-06)

The freshness/push-token runtime is **implemented and tested** (the old generation/watermark/demand model and TypeScript-simulation-only era are gone from the backend). The backend + CLI are near-complete, and the **web UI is built** (read-mostly Next.js, polls the Catchment — see Web UI below). The **playground was extracted** to a standalone `playground/` (in-memory sim). Known next step: **failure/retry handling** (see Fault tolerance below).

## Structure

```
src/duckstring/
  core.py                  # Pond/Ripple runtime handles + @ripple decorator (used by deployed pond code)
  engine/                  # PURE orchestration engine (no FastAPI/DB/HTTP). The state machine.
    core.py                #   shared dataclasses: NEVER, Window, Pond, Ripple, Trigger, BeginRun, Pond/RippleState
    catchment.py           #   the FULL engine (Ponds + Ripples, pull + push) — the Catchment's brain
    worker.py              #   push-only WorkerEngine — the Duck's engine (executes a Pond Run to completion)
    pond.py                #   the per-Pond run LEDGER (SQLite at ponds/{base_pond}/pond.db)
    __init__.py            #   re-exports the composed API; tests/test_engine.py is the behaviour gate
  duck/                    # The Duck: per-Pond worker process (intra-Pond push execution)
    core.py                #   DuckCore: WorkerEngine + ledger + outgoing event buffer (transport-free)
    executor.py            #   RippleExecutor (thread pool; ripple loading + parquet export) + load_topology
    client.py              #   CatchmentClient (HTTP: poll jobs, post events)
    __main__.py            #   `python -m duckstring.duck ...` serve loop
  catchment/               # The Catchment: FastAPI runtime
    app.py                 #   create_app + lifespan (starts Driver, scheduler, resume_incomplete)
    driver.py              #   Driver: engine brain + Duck coordinator + persistence + trigger/window CRUD + restart restore
    launcher.py            #   SubprocessLauncher (spawns Ducks) / NoopLauncher (tests)
    db.py                  #   SQLite connect + migration runner
    schema/001_init.sql    #   Database schema (see below)
    routes/                #   deploy, orchestrate (triggers/status/runs/windows), duck (jobs/events), data, catchment (health)
    registry.py, dag.py    #   pond DuckDB registry paths; inter-pond cycle check
  cli/                     # Typer CLI (`duckstring` / `ds`)
    trigger.py, window.py  #   tap/pulse/wave/tide/start/stop/remove ; trigger window add/list/remove
    status.py, deploy.py, data.py, pond.py, catchment.py, config.py, _http.py
docs/guide/                # Design documentation (theory.md is authoritative)
frontend/                  # The live Catchment web UI (Next.js; static export served at catchment/static). See Web UI.
  src/lib/                 #   api.ts (HTTP client), store.ts (zustand poll store + colour palette), types.ts
  src/components/          #   DagCanvas, Pond/Ripple/TriggerNode, Sidebar, RunHistory, WindowEditor, TraceChart
playground/                # Standalone in-memory simulation (own repo → playground.duckstring.com); src/lib/orchestration.ts is its engine
```

## Runtime architecture (two-tier: Catchment + Ducks)

- **The Catchment owns pull.** It runs the **full** engine (`engine/catchment.py`: Ponds *and* Ripples, pull + push), holds triggers/windows, and decides Pond Runs. Modelling ripples is required — the Tap-3/1 result and the bottleneck cadence come from *ripple-level* pull. `start_pond_run` records a `BeginRun(pond, F)` on `state.pending_begin_runs`; `Driver` drains these and dispatches them.
- **Each executing Pond runs a "Duck"** (`duck/`, one subprocess per Pond, `SubprocessLauncher`). Given `begin_run(F)` it pushes every Ripple to `F` (push-only, `engine/worker.py`), executes ripple functions, and reports `ripple`/`run_completed` events. It is spawned on the first run, killed when the Pond is idle (kept warm while a standing trigger is active), and **survives Catchment downtime** (finishes in-flight runs from its ledger + engine, buffers events, replays idempotently on reconnect).
- **No cap** on concurrent Pond Runs — completions clock the pull cascade; that is the flow control.
- **Transport**: Duck→Catchment is REST POST (`/api/duck/{pond}/events`); Catchment→Duck is a short-poll the Duck holds (`/api/duck/{pond}/jobs`). The Duck always dials back, so the same code works local and (future) remote — remote is just a different launcher. `DUCKSTRING_CATCHMENT_URL` tells Ducks where to dial; `DUCKSTRING_DISABLE_DUCKS=1` swaps in `NoopLauncher` (tests exercise the engine + persistence without spawning processes).
- Cross-Pond data: each Pond writes its tables to `ponds/{name}/data/{table}.parquet` (atomic tmp+replace); sinks read those parquet files. Per-Pond DuckDB registry at `ponds/{name}/registry.duckdb`.

## Triggers & demand control (CLI → `/api/outlets/{name}/…` → Driver)

- **tap** (one pull), **wave** (standing pull), **pulse** (push `now`, propagates upstream), **tide** (standing push; a **staleness bound in seconds**, not cron).
- **start** — inject demand on the Pond alone: a push target of `NEVER` ("-Inf"), so it runs once against current inputs with **no** upstream propagation (`engine.start_pond`).
- **stop** — clear the Pond's push+pull and its Ripples' **pull**, but KEEP Ripple push so started runs complete; also cancels the standing trigger. `--upstream` propagates to all ancestors.
- **remove** — drop only the standing Wave/Tide trigger (existing work drains).
- One-shot CLI commands (tap/pulse/start) open the live status until the target settles to idle; standing ones stay open.

## Windows (batch availability on Inlets)

RFC-5545-flavoured recurrence (no cron anywhere — `croniter` is gone). `engine.core.Window(start_anchor, duration, freq_unit ∈ {SECOND,MINUTE,HOUR,DAY,WEEK}, freq_interval, valid_days ⊆ {MON..SUN}|None, until)`. Occurrences are `start_anchor + k·delta` filtered by valid_days/until; a Pond is "fresh until" the active window's end, with `D` = window duration. `Window.active_end`/`next_boundary` are O(1) (used per-tick in `pond_source_f`/`next_wake`); `Window.occurrences` (bounded) is used only for add-time overlap validation. Managed via `duckstring trigger window {pond} add|list|remove` (`cli/window.py`); operational config (CLI/API, survives redeploys), not declared in `pond.toml`. `add` requires only `--name`/`--every` (`--start` defaults to 00:00 today; `--duration` defaults to `--every` = back-to-back).

## Web UI (`frontend/`) + playground

The Catchment serves a **read-mostly Next.js UI** (static export, mounted at `/`; `npm run dev` proxies `/api` to a Catchment, default `:7474`). It polls `GET /api/status` (~1 s) and `GET /api/runs`, and POSTs the `trigger` surface; **topology is read-only** (Ponds come from deploying code — never UI-authored).

- **`/api/status`** is enriched beyond the CLI's needs (`driver.status()`): per-Pond `d_ms` + standing `trigger`, and per-Ripple state + intra-Pond `ripple_edges` + `runs_completed`, so the UI can render the nested Ripple sub-graph live.
- **`/api/runs`** (`driver.run_history`) is the run-history feed: newest-first Pond Runs with params `pond` / `lineage` (**upstream-only**) / `ripples` (nest Ripple Runs) / `limit` (≤1000).
- Data layer in `frontend/src/lib/`: `api.ts` (typed client), `store.ts` (zustand poll store; growing-window run feed; the semantic colour palette `THEME_*` + helpers `stateColor`/`consumeEdgeColor`/`nodeFill`/`formatAge`), `types.ts`. **Colours are centralised in `store.ts`** — node fill is a wash of the rim colour; the brand cyan is the running state; pull=amber, push=green-yellow.
- Built with **Next 16** — heed `frontend/AGENTS.md` (breaking changes; read `node_modules/next/dist/docs/` before editing frontend code).

The **playground** (`playground/`) is the standalone in-memory sim (the old `frontend/` content), bound for its own repo + `playground.duckstring.com`; it shares no code with the product UI.

## Fault tolerance (current state — relevant to the next session)

- **Duck**: in-flight Pond Runs complete without the Catchment; events buffer and replay (idempotent on freshness `F`); on (re)start the Duck reconciles against its ledger and re-runs **only incomplete Ripples**.
- **Catchment restart**: `Driver.reload` rebuilds engine state from SQLite (demand/freshness from `pond_state`/`pond_target`, `gen` from `pond_run` counts, per-Ripple `end_f` from successful `ripple_run` rows), and `resume_incomplete` (called from the lifespan) re-dispatches any `pond_run` left `status='running'`. `on_event` stamps a Ripple's `start_f` from the event freshness so replayed/resumed completions record correctly.
- **Known gap (next session)**: ripple/run **failure handling** is not implemented in the new runtime. The Duck logs a ripple error but does not report failure status, and the Catchment does not retry. The `immediate_retries`/`source_retries` columns on `pond_version` (and `retry` on `pond_run`/`ripple_run`) exist but are not yet acted upon.

## Catchment database

SQLite `duck.db` at the catchment root. Schema in `catchment/schema/001_init.sql`, applied by `catchment/db.py:migrate()`. New migrations are numbered SQL files (`002_*.sql`, …). All file paths stored in the DB are **relative to the catchment root**. Freshness/targets are stored as UTC ISO-8601 text.

### Schema (identity is a three-table split)

- **`pond_name`** — the abstract named entity (`name`, `kind` ∈ inlet/pond/outlet, `git_branch`).
- **`pond_version`** — a specific deployed snapshot (`pond_name_id`, `version`, `major`, `source_path`, retry config). Immutable artifact; topology + run history key off this.
- **`pond`** — the **selected** version, one per `(pond_name, major)` → `pond_version` (upserted on deploy). This is "the Pond" and the FK target for all live demand/freshness/graph tables.
- Topology (keyed on `pond_version`): `ripple`, `ripple_to_ripple` (intra-pond edges, all required).
- Live state (keyed on `pond`): `pond_to_pond` (sink `pond_id` → source `pond_name_id` + `source_major`, so a sink can deploy before its source), `pond_state` (start_f/end_f/d_ms/has_pull/has_received_pull), `pond_target` (push target set), `pond_window` (PK `(pond_id, name)`), `pond_trigger` (PK `pond_id`; kind wave/tide, bound_ms).
- History (keyed on `pond_version` + freshness `f`): `pond_run`, `ripple_run`. `started_at`/`finished_at` are the Duck's wall-clock execution span (the Duck reports both on the `ripple` event; the Catchment records them) — that's where the UI's run durations come from. All timestamps are UTC ISO-8601 (tz-aware).
- The **per-Pond run ledger is NOT in `duck.db`** — it lives at `ponds/{base_pond}/pond.db` (owned by `engine/pond.py`): the Duck's operational/recovery record (`ripple_run_state`, `pond_run`). The Catchment's `pond_run`/`ripple_run` are the canonical history.

## Orchestration model (theory.md is authoritative)

Freshness-based Kanban. The Pond is a packaging/versioning boundary; its `start`/`end` are zero-duration boundary nodes carried as Pond state, so Pond and Ripple share the same rules.

- **Freshness `F`** — a UTC timestamp per node: the run-start time of the oldest root feeding it (with windows, the "fresh until" window end). `NEVER` (`datetime.min`) is the sentinel for never-run. Staleness = `now + D - F`.
- **Pull** (Tap/Wave) — a `hasPull` token; a node runs when a parent is fresher (`sourceF > startF`) and re-arms parents on start. Cold-start guards use **startF** (`source.startF <= this.startF`).
- **Push** (Pulse/Tide/start) — a **set** of unsatisfied target freshnesses; run when `sourceF >= min(targets)`, clearing every target reached. Pond run start stamps every Ripple with `Pond.startF`.
- The four hard-won landmines (startF cold-start guards, push target *set*, Tide `max(targets) ?? startF` clock ref, the run-start ripple stamp) are encoded in `engine/` and guarded by `tests/test_engine.py` — preserve them.

## Testing

`pytest` (budgets via `pytest-timeout`; `timeout = 1` default in `pyproject.toml`, sim/integration tests override). Pure-engine tests are behavioural simulations driving `sentinel`/`tick` over sim-time (100 ms step, **never sleep**). Session env in `tests/conftest.py`: `DUCKSTRING_SLEEP_MULTIPLIER=0.01`, `DUCKSTRING_DISABLE_DUCKS=1`. Notable suites: `test_engine` (validated engine), `test_engine_split`, `test_duck` (buffer/replay/recovery), `test_restart` (restart restore), `test_window`, `test_runtime` (**e2e: real subprocess Ducks** on the demo ponds; enables Ducks + a live server). Demo ponds in `src/duckstring/demo/` (transactions, products → sales → reports; bottleneck = sales.join 3 s).

## Before finishing any code change

Run `ruff check .` and fix all errors (line-length 128; E/F/I/B).

## Conventions

- Table names: **singular** (`pond`, not `ponds`).
- Association tables: **`{parent}_to_{child}`** for many-to-many; `{child}_in_{parent}` for nesting.
- FK columns: **`{table}_id`**; qualify (`sink_id`, `source_id`) when two FKs reference the same table.
- Inter-pond and intra-pond concerns stay in separate tables — do not unify them.
- Freshness/demand state is keyed on `pond` (the selected version); topology and run history on `pond_version`.
