# Duckstring

A packaging standard for data transforms. Each transform is a versioned **Pond** (Python package) that declares its parent Ponds in `pond.toml`. The pipeline is implicit in the package graph. **Ripples** are the execution units within a Pond. The **Catchment** (FastAPI server) is the reference runtime — a convenience, not the product.

`docs/docs/theory.md` is the **authoritative orchestration spec** — its "Pond State Variables" pseudocode is the exact state machine. `playground/src/lib/orchestration.ts` is a well-tested TypeScript *simulation* (the standalone playground); the Python engine is a faithful, behaviour-for-behaviour port of it. The rest of `docs/` is the Docusaurus documentation site (→ docs.duckstring.com), written against the real CLI/API surface — update it when that surface changes.

## Brand & Positioning

(The old `brand/` directory was removed from the repo — internal strategy doesn't belong in OSS. These are the settled rules.)

- **Never describe Duckstring as an orchestration framework.** That positions it against Airflow/Prefect/Dagster. The differentiation is the package model, not the execution model. **Never mention competitors by name in copy or docs.**
- **The Catchment is not the product.** Don't lead with it in copy or docs introductions. It's the batteries-included runtime for teams that want the full stack.
- **Target audience**: data engineers who have hit the coordination and ownership walls of large transform pipelines — specifically those who've adopted or considered a mesh pattern and found that breaking changes still require organisation-wide coordination. They've reasoned their way to needing versioned package boundaries; they just don't have SemVer or concurrent version execution yet. Mesh-pattern users are the warmest first audience.
- **Tagline**: "There is no DAG." — the DAG exists but is implicit in the package graph. You don't build or govern it. **Package description** (pyproject/PyPI): "Build data pipelines the way you build software: version each transform, declare its dependencies, and Duckstring resolves the execution DAG automatically."
- **Name the gaps honestly** — single-node scope (~<50M rows) and the unimplemented Trickle (incremental) concept are stated, not downplayed; engineers find them immediately anyway.
- The README is deliberately short (positioning → concepts → one golden-path quickstart → trigger table → docs links); examples live in `docs/`, not the README — duplicated examples drift, and the PyPI copy of a stale README is immutable per release. The README's opening paragraphs are the author's own wording — don't rewrite them.

## Current state (2026-06)

The freshness/push-token runtime is **implemented and tested** (the old generation/watermark/demand model and TypeScript-simulation-only era are gone from the backend). The backend + CLI are complete, and the **web UI is built** (read-mostly Next.js, polls the Catchment — see Web UI below). The **playground was extracted** to a standalone `playground/` (in-memory sim). **Fault tolerance is now implemented end-to-end** (immediate + on-change retries, failed/killed/blocked states, dead/silent-Duck detection, error+traceback surfacing — see Fault tolerance). The old `docs/guide/` design docs were removed; `docs/` is now a fully-written Docusaurus site (`docs/docs/theory.md` carried over — its Fault-Tolerance section IS current). **Puddles (local pre-deploy testing) are implemented** — see Puddles below.

**Concurrent major versions are implemented** (v0.2.0): the runtime Pond identity is the **pond key `"{name}@{major}"`** (`keys.py`) — each deployed major line is an independent live Pond with its own engine node, Duck, and storage (`ponds/{name}/m{major}/{registry.duckdb,data/,pond.db}`). Sinks wire to the Source major their `[sources]` pin selects; `Pond.read_table` resolves it via `source_majors` (computed by the Duck from the deployed `pond.toml`; absent for puddle runs → flat layout). All pond-targeting routes take optional `major`/`version` query params — `major` picks the line (default: highest deployed), `version` must be that line's *selected* artifact (422 otherwise) — resolved by `Driver.resolve`; the CLI `--major`/`-m`, `--version`/`-v` pass through everywhere; `/api/status` entries carry `id`/`name`/`major` and `edges` use ids. `min_version` from `[sources]` is still stored-but-unenforced. **Auth is two-model**: platform auth (recommended for hosted — the platform gates requests; the UI rides its session cookies; Ducks dial localhost inside the sandbox; the CLI attaches per-catchment custom headers via `catchment connect --header 'Name: value'`, stored in config and merged by `config.auth_headers`) and the built-in API key for bare self-hosting (`create_app(root, api_key=...)` / `DUCKSTRING_API_KEY` gates every `/api` route except `/api/health`, Bearer or X-Duck-Token; Ducks inherit the key via the launcher token; `init --key` or `init --generate-key` — generated, printed once, stored in the registration; the web UI prompts for the key on 401 and keeps it in localStorage). `_http.get/post` take `auth=cfg` (the registration dict). config.toml is chmod 0600. **The static UI is subpath-hostable** (e.g. Posit Connect `/content/{guid}/`): `assetPrefix: "./"` + a runtime-derived API base in `frontend/src/lib/api.ts`; `tests/test_static.py` + the release workflow guard against absolute asset paths. **Platform hosting is first-class**: `duckstring.catchment.asgi` is the env-configured entry (`DUCKSTRING_ROOT`, default `./.duckstring` — survives restarts but not redeploys of the bundle; run exactly ONE app process). **State download**: `duckstring catchment download [--path DIR=./.duckstring]` pulls the whole root via `GET /api/catchment/archive` (streamed uncompressed tar; SQLite snapshotted via the backup API, `-wal/-shm` skipped; DuckDB registries copied as-is — download while quiescent) after a size confirmation from `GET /api/catchment/usage`; the default path drops into a deploy bundle so state survives a platform redeploy (docs: running-a-catchment "Surviving a redeploy"). The Duck dial-back address is no longer assumed: `create_app(root, base_url=...)` (the CLI passes its bind address — this fixed `catchment init --port` spawning Ducks at :7474), env `DUCKSTRING_CATCHMENT_URL`, or None → `SubprocessLauncher` defers spawns (pending keys report `is_running` so liveness won't fail them) until a middleware learns the bound address from the first request's ASGI scope (`tests/test_platform.py`). **Release automation**: `release.yml` builds the frontend + dists on a `v*` tag and publishes via PyPI Trusted Publishing (the `pypi` GitHub environment + PyPI publisher must be configured once).

## Structure

```
src/duckstring/
  core.py                  # Pond/Ripple handles + @ripple/@puddle decorators + Catchment client + pond.toml/entrypoint/import helpers
  engine/                  # PURE orchestration engine (no FastAPI/DB/HTTP). The state machine.
    core.py                #   shared dataclasses: NEVER, Window, Pond, Ripple, Trigger, BeginRun, Pond/RippleState
    catchment.py           #   the FULL engine (Ponds + Ripples, pull + push) — the Catchment's brain
    worker.py              #   push-only WorkerEngine — the Duck's engine (executes a Pond Run to completion)
    pond.py                #   the per-Pond run LEDGER (SQLite at ponds/{name}/m{major}/pond.db)
    __init__.py            #   re-exports the composed API; tests/test_engine.py is the behaviour gate
  duck/                    # The Duck: per-Pond worker process (intra-Pond push execution)
    core.py                #   DuckCore: WorkerEngine + ledger + outgoing event buffer (transport-free)
    executor.py            #   RippleExecutor (thread pool; ripple loading + parquet export) + load_topology
    client.py              #   CatchmentClient (HTTP: poll jobs, post events)
    __main__.py            #   `python -m duckstring.duck ...` serve loop
  catchment/               # The Catchment: FastAPI runtime
    app.py                 #   create_app + lifespan (starts Driver, scheduler, resume_incomplete)
    asgi.py                #   env-configured ASGI entry for platform hosting (Posit Connect etc.)
    driver.py              #   Driver: engine brain + Duck coordinator + persistence + trigger/window CRUD + restart restore
    launcher.py            #   SubprocessLauncher (spawns Ducks) / NoopLauncher (tests)
    db.py                  #   SQLite connect + migration runner
    schema/001_init.sql    #   Database schema (see below)
    routes/                #   deploy, orchestrate (triggers/control/status/runs/windows), duck (jobs/events), data (parquet), catchment (health)
    registry.py, dag.py    #   pond DuckDB registry paths; inter-pond cycle check
  local/                   # Local pre-deploy testing (no engine/FastAPI/Ducks). See Puddles.
    project.py             #   load_project: pond.toml + entrypoints + puddles/ dirs
    hydrate.py             #   materialise @puddle definitions → puddles/ponds/{source}/data/*.parquet
    runner.py              #   run_pond: one local Pond Run in topo order → puddles/out/
  cli/                     # Typer CLI (`duckstring` / `ds`)
    trigger.py             #   tap/pulse/wave/tide/remove ; window add/list/remove (cli/window.py)
    control.py             #   wake/sleep/force/kill/clear/failure-budget (a Pond's execution & health)
    pond.py, deploy.py     #   pond init/demo/hydrate/run/deploy
    puddle.py              #   puddle ls/show/query (inspect ./puddles via in-memory DuckDB views)
    status.py, data.py, catchment.py, config.py, window.py, _http.py
docs/                      # Docusaurus docs site → docs.duckstring.com (content in docs/docs/; theory.md is authoritative)
frontend/                  # The live Catchment web UI (Next.js; static export served at catchment/static). See Web UI.
  src/lib/                 #   api.ts (HTTP client), store.ts (zustand poll store + colour palette), types.ts
  src/components/          #   DagCanvas, Pond/Ripple/TriggerNode, Sidebar, RunHistory, WindowEditor, TraceChart
playground/                # Standalone in-memory simulation (own repo → playground.duckstring.com); src/lib/orchestration.ts is its engine
```

## Runtime architecture (two-tier: Catchment + Ducks)

- **The Catchment owns pull.** It runs the **full** engine (`engine/catchment.py`: Ponds *and* Ripples, pull + push), holds triggers/windows, and decides Pond Runs. Modelling ripples is required — the Tap-3/1 result and the bottleneck cadence come from *ripple-level* pull. `start_pond_run` records a `BeginRun(pond, F)` on `state.pending_begin_runs`; `Driver` drains these and dispatches them.
- **Each executing Pond runs a "Duck"** (`duck/`, one subprocess per pond key `name@major`, `SubprocessLauncher`). Given `begin_run(F)` it pushes every Ripple to `F` (push-only, `engine/worker.py`), executes ripple functions, and reports `ripple`/`run_completed` events. It is spawned on the first run, killed when the Pond is idle (kept warm while a standing trigger is active), and **survives Catchment downtime** (finishes in-flight runs from its ledger + engine, buffers events, replays idempotently on reconnect).
- **No cap** on concurrent Pond Runs — completions clock the pull cascade; that is the flow control.
- **Transport**: Duck→Catchment is REST POST (`/api/duck/{name}/{major}/events`); Catchment→Duck is a short-poll the Duck holds (`/api/duck/{name}/{major}/jobs`). The Duck always dials back, so the same code works local and (future) remote — remote is just a different launcher. `DUCKSTRING_CATCHMENT_URL` tells Ducks where to dial; `DUCKSTRING_DISABLE_DUCKS=1` swaps in `NoopLauncher` (tests exercise the engine + persistence without spawning processes).
- Cross-Pond data: each major line writes its tables to `ponds/{name}/m{major}/data/{table}.parquet` (atomic tmp+replace); sinks read the parquet of the Source major they pin. Per-line DuckDB registry at `ponds/{name}/m{major}/registry.duckdb`.

## Triggers & control (CLI → `/api/ponds/{name}/…` → Driver)

(Routes still live at `/api/ponds/{name}/…` and accept *any* deployed Pond, not only Outlets.)

**Triggers** (`cli/trigger.py` — demand signals):
- **tap** (one pull), **wave** (standing pull), **pulse** (push `now`, propagates upstream), **tide** (standing push; a **staleness bound** like `30s`/`1d`, not cron).
- **remove** — drop the standing Wave/Tide trigger (existing work drains).

**Control** (`cli/control.py` — a Pond's execution & health; see Fault tolerance):
- **wake** (`engine.wake_pond`) — a one-shot **non-propagating** pull: runs once when Sources are already fresher (`sourceF > startF`), without soliciting them. Clears failure/kill.
- **force** (`engine.force_pond`) — recompute now at the *current* freshness even with no upstream change (resets the Pond's + Ripples' `endF` so they re-run); does **not** advance freshness, so it does not propagate downstream. Clears failure/kill.
- **sleep** (`engine.sleep_pond`, the old `stop`) — clear push+pull and Ripples' pull, KEEP Ripple push so started runs complete; cancels the standing trigger; `--upstream` reaches ancestors.
- **kill** (`engine.kill_pond`) — terminate the Duck process and park the Pond **killed** (terminal, supersedes retries) until a wake/force/clear.
- **clear** (`engine.clear_pond`) — reset a failed/killed Pond (no run); abandons the halted Run's phantom (`start_f → end_f`) so liveness won't re-fail it, and unblocks downstream.
- **failure-budget** — show / set the live retry budgets (`--immediate`, `--on-change`).
- One-shot commands (tap/pulse/wake/force) open the live status until the target settles (idle/failed/killed/blocked); standing ones stay open.

## Windows (batch availability on Inlets)

RFC-5545-flavoured recurrence (no cron anywhere — `croniter` is gone). `engine.core.Window(start_anchor, duration, freq_unit ∈ {SECOND,MINUTE,HOUR,DAY,WEEK}, freq_interval, valid_days ⊆ {MON..SUN}|None, until)`. Occurrences are `start_anchor + k·delta` filtered by valid_days/until; a Pond is "fresh until" the active window's end, with `D` = window duration. `Window.active_end`/`next_boundary` are O(1) (used per-tick in `pond_source_f`/`next_wake`); `Window.occurrences` (bounded) is used only for add-time overlap validation. Managed via `duckstring trigger window {pond} add|list|remove` (`cli/window.py`); operational config (CLI/API, survives redeploys), not declared in `pond.toml`. `add` requires only `--name`/`--every` (`--start` defaults to 00:00 today; `--duration` defaults to `--every` = back-to-back).

## Puddles (local pre-deploy testing — `local/`, `cli/puddle.py`)

A **Puddle** is a code-defined snapshot of a Source table, for testing a Pond before deployment. Definitions in `src/puddles.py` (untyped `@puddle("source.table")` or whole-source `@puddle("source")`; handle `p` has `con`/`path`/`write_table`/`write_path`/`catchment()` — see `core.py`). `duckstring pond hydrate` imports them (decorator side-effect, like `@ripple`) and materialises `puddles/ponds/{source}/data/{table}.parquet` — the catchment-root layout, so `Pond.read_table`'s foreign branch works unchanged with `root=puddles/`. Missing definitions skip with a warning (`--from-catchment` fills from the Catchment). `duckstring pond run [--ripple X] [--fresh]` is **one local Pond Run** (sequential topo order, no engine/freshness/Ducks): full runs reset `puddles/out/` (registry + exported parquet); a **self-puddle** (`puddles/ponds/{this_pond}/`) is copied in as the seed first, making incremental reruns idempotent. `duckstring puddle ls|show|query` inspects `./puddles` via in-memory DuckDB views (`"{pond}"."{table}"`; output overrides a same-named self-puddle). Entrypoints are declarable in `pond.toml` (`[pond] ripples`/`puddles`, defaults `src/pond.py`/`src/puddles.py`) and honoured by deploy + the Duck executor via `core.import_pond_module`. Tests: `tests/test_puddle.py`; demo `sales` carries a worked `src/puddles.py`.

## Web UI (`frontend/`) + playground

The Catchment serves a **read-mostly Next.js UI** (static export, mounted at `/`; `npm run dev` proxies `/api` to a Catchment, default `:7474`). It polls `GET /api/status` (~1 s) and `GET /api/runs`, and POSTs the trigger + control surface; **topology is read-only** (Ponds come from deploying code — never UI-authored).

- **`/api/status`** is enriched beyond the CLI's needs (`driver.status()`): per-Pond `d_ms` + standing `trigger`, per-Ripple state + intra-Pond `ripple_edges` + `runs_completed`, and the fault/control fields `is_failed`/`is_killed`/`is_blocked`/`failed_f`/`failures`/`immediate_retries`/`source_retries`. The per-Pond `status` string has failure precedence: **failed → killed → blocked → running → queued → idle**.
- **`/api/runs`** (`driver.run_history`) is the run-history feed: newest-first Pond Runs with params `pond` / `lineage` (**upstream-only**) / `ripples` (nest Ripple Runs) / `limit` (≤1000). Each run + ripple carries `status`, `retry` (attempt index), `error`, and `traceback`.
- **Bottom panel** is split 50/50: `RunHistory` (left, clickable rows) + `RunDetail` (right). RunDetail shows the run's freshness/timing, the per-attempt Ripple list (the retry trace via `↻N`), and **below it** the failure(s) — one entry per source (`<ripple> · message`, or `Pond · message`) with the full `traceback` in a `<pre>`. The Sidebar's Control row is Force/Wake/Sleep/Kill (4-up, matching the Trigger row); a Failures section sets the retry budgets and shows Clear Failure when failed.
- **`/api/data`** (`routes/data.py`) reads each Pond's **exported Parquet** (`ponds/{pond}/data/*.parquet`) via an in-memory DuckDB connection — never the live registry — so a data query never contends with a running Duck.
- Data layer in `frontend/src/lib/`: `api.ts` (typed client), `store.ts` (zustand poll store; growing-window run feed; the semantic colour palette `THEME_*` + helpers `stateColor`/`consumeEdgeColor`/`nodeFill`/`formatAge`), `types.ts`. **Colours are centralised in `store.ts`** — node fill is a wash of the rim colour; the brand cyan is the running state; pull=amber, push=green-yellow.
- Built with **Next 16** — heed `frontend/AGENTS.md` (breaking changes; read `node_modules/next/dist/docs/` before editing frontend code).

The **playground** (`playground/`) is the standalone in-memory sim (the old `frontend/` content), bound for its own repo + `playground.duckstring.com`; it shares no code with the product UI.

## Fault tolerance (implemented — `theory.md` "Fault Tolerance" is authoritative)

Two retry budgets (default 0), live on the Pond and editable via `control failure-budget` (`pond_retry` table; seeded on deploy from the `pond.toml` / `pond_version` defaults, then operator-owned):
- **immediate_retries** — Ripple-Run retries *within* one Pond Run (per-frontier, consumed by the Duck via `worker.immediate_left`).
- **source_retries** (on-change) — whole Pond Runs the Catchment re-attempts when a Source updates.

Pond fault state on `PondState`: `is_failed` (a Run gave up, not yet superseded), `failed_f` (freshest failed Run — the recovery watermark; the run-gate keys on `start_f`, this is for clearing/telemetry), `failures` (count vs `source_retries`), `is_blocked` (a required Source is failed/killed/blocked — **derived and propagated** downstream via `derive_blocked`), `is_killed` (operator Kill; terminal). Gating: a failed Pond only re-runs via the on-change path (`sourceF > startF`, while `failures <= source_retries`); a Run completing fresher than `failed_f` clears the episode; a **blocked-but-not-failed** Pond still drains existing Source output but never solicits; a killed Pond is fully gated.

Failure sources (every type produces a **message**; ripple/Duck exceptions also a **traceback** — surfaced in Run Detail):
- **Ripple error** → Duck spends `immediate_retries` (per-frontier), then reports `failed(F = ripple.startF, error, traceback)`; the Catchment fails the Pond at that Run, counts it, blocks downstream (`engine.fail_ripple`).
- **Duck-level error** (e.g. ledger write) → the Duck reports `pond_failed` (attributed to its last `begin_run`) and exits.
- **Dead / silent Duck** → `Driver._check_liveness` (in `scheduler_tick`, only for `SubprocessLauncher`) fails an in-flight Pond whose process is gone (`proc.poll()`), or whose last contact aged past 60 s (`engine.fail_pond` at `start_f`).
- **Stuck Run** → the Duck's watchdog reports `pond_failed` if it has outstanding work but no Ripple running for 30 s.
- **Kill** → `Driver.kill` terminates the Duck and parks the Pond `killed`.

Recovery: a failed Pond with on-change budget re-runs on the next Source change (respawning a Duck); redeploying a fixed artifact auto-clears the failure (`Driver.clear_on_redeploy`); `control clear` / `force` / `wake` clear it manually. Run history records **one row per attempt** (`ripple_run` keyed on `retry`) with `error` + `traceback`. `_check_liveness` skips failed/killed/blocked Ponds; `clear` rolls `start_f → end_f` so the abandoned Run isn't re-failed.

Concurrency hardening: SQLite connects with `PRAGMA busy_timeout`; DuckDB writes + the Parquet export retry transient locks via `core.retry_on_lock` (export uses a read-write connection, never `read_only`, to avoid clashing with pipelined Ripple connections).

Duck/restart resilience (pre-existing): in-flight Runs complete without the Catchment (events buffer + replay idempotently on `F`); on (re)start the Duck reconciles against its ledger and re-runs **only incomplete Ripples**. `Driver.reload` rebuilds engine state from SQLite (demand/freshness from `pond_state`/`pond_target` incl. the fault fields + `pond_retry`, `gen` from `pond_run` counts, per-Ripple `end_f` from successful `ripple_run` rows); `resume_incomplete` re-dispatches `pond_run` left `status='running'`.

## Catchment database

SQLite `duck.db` at the catchment root. Schema in `catchment/schema/001_init.sql`, applied by `catchment/db.py:migrate()`. New migrations are numbered SQL files (`002_*.sql`, …). All file paths stored in the DB are **relative to the catchment root**. Freshness/targets are stored as UTC ISO-8601 text.

### Schema (identity is a three-table split)

- **`pond_name`** — the abstract named entity (`name`, `kind` ∈ inlet/pond/outlet, `git_branch`).
- **`pond_version`** — a specific deployed snapshot (`pond_name_id`, `version`, `major`, `source_path`, retry config). Immutable artifact; topology + run history key off this.
- **`pond`** — the **selected** version, one per `(pond_name, major)` → `pond_version` (upserted on deploy). This is "the Pond" and the FK target for all live demand/freshness/graph tables.
- Topology (keyed on `pond_version`): `ripple`, `ripple_to_ripple` (intra-pond edges, all required).
- Live state (keyed on `pond`): `pond_to_pond` (sink `pond_id` → source `pond_name_id` + `source_major`, so a sink can deploy before its source), `pond_state` (start_f/end_f/d_ms/has_pull/has_received_pull **+ is_failed/is_blocked/failed_f/failures/is_killed/pull_local**), `pond_target` (push target set), `pond_retry` (immediate_retries/source_retries — live budgets), `pond_window` (PK `(pond_id, name)`), `pond_trigger` (PK `pond_id`; kind wave/tide, bound_ms).
- History (keyed on `pond_version` + freshness `f`): `pond_run` (`status` ∈ running/success/failed/killed, `error`, `traceback`) and `ripple_run` (**PK includes `retry`** — one row per attempt = the retry trace; `status`, `error`, `traceback`). `started_at`/`finished_at` are the Duck's wall-clock execution span (reported on the `ripple` event) — the UI's run durations. All timestamps are UTC ISO-8601 (tz-aware).
- The **per-Pond run ledger is NOT in `duck.db`** — it lives at `ponds/{name}/m{major}/pond.db` (owned by `engine/pond.py`): the Duck's operational/recovery record (`ripple_run_state`, `pond_run`). The Catchment's `pond_run`/`ripple_run` are the canonical history.

## Orchestration model (theory.md is authoritative)

Freshness-based Kanban. The Pond is a packaging/versioning boundary; its `start`/`end` are zero-duration boundary nodes carried as Pond state, so Pond and Ripple share the same rules.

- **Freshness `F`** — a UTC timestamp per node: the run-start time of the oldest root feeding it (with windows, the "fresh until" window end). `NEVER` (`datetime.min`) is the sentinel for never-run. Staleness = `now + D - F`.
- **Pull** (Tap/Wave) — a `hasPull` token; a node runs when a parent is fresher (`sourceF > startF`) and re-arms parents on start. Cold-start guards use **startF** (`source.startF <= this.startF`).
- **Push** (Pulse/Tide) — a **set** of unsatisfied target freshnesses; run when `sourceF >= min(targets)`, clearing every target reached. Pond run start stamps every Ripple with `Pond.startF`. (The control verbs map onto these: **wake** = a non-propagating one-shot pull, **force** = a same-freshness recompute, **sleep** = clear demand, **kill** = terminate.)
- The four hard-won landmines (startF cold-start guards, push target *set*, Tide `max(targets) ?? startF` clock ref, the run-start ripple stamp) are encoded in `engine/` and guarded by `tests/test_engine.py` — preserve them.

## Testing

`pytest` (budgets via `pytest-timeout`; `timeout = 5` default in `pyproject.toml`, sim/integration tests override). Pure-engine tests are behavioural simulations driving `sentinel`/`tick` over sim-time (100 ms step, **never sleep**). Session env in `tests/conftest.py`: `DUCKSTRING_SLEEP_MULTIPLIER=0.01`, `DUCKSTRING_DISABLE_DUCKS=1`. Notable suites: `test_engine` (validated engine), `test_engine_split`, `test_duck` (buffer/replay/recovery), `test_restart` (restart restore), `test_window`, `test_runtime` (**e2e: real subprocess Ducks** on the demo ponds; enables Ducks + a live server). Demo ponds in `src/duckstring/demo/` (transactions, products → sales → reports; bottleneck = sales.join 3 s).

## Before finishing any code change

Run `ruff check .` and fix all errors (line-length 128; E/F/I/B).

## Conventions

- Table names: **singular** (`pond`, not `ponds`).
- Association tables: **`{parent}_to_{child}`** for many-to-many; `{child}_in_{parent}` for nesting.
- FK columns: **`{table}_id`**; qualify (`sink_id`, `source_id`) when two FKs reference the same table.
- Inter-pond and intra-pond concerns stay in separate tables — do not unify them.
- Freshness/demand state is keyed on `pond` (the selected version); topology and run history on `pond_version`.
- **`pond.f`** exposes the run's freshness to ripple code (Duck passes each ripple's `start_f` through the executor; the local runner stamps one `now()` per run) — the replay-stable watermark/provenance stamp (crash replay + immediate retries re-run at the same F). Documented in python-api.md + the Incremental Ripples guide; first brick of Trickle.
- **No DuckDB replacement scans in Ripple/demo/docs code** (referencing a Python local as a SQL table name, `FROM raw`): it resolves by scanning Python frames and is flaky under the Duck's threaded executor ("don't know what type:" failures on CI). `Pond.read_table` registers foreign Source tables as temp views named after the table — SQL references the table name directly; own tables are queried directly; compose relations via the relation API (`.union`, …) otherwise.
