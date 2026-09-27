# Duckstring

**Working with the author:** ask questions in plain prose, not the multiple-choice question UI.

Duckstring is an open source data engineering platform built on DuckDB. Transformations are packaged as versioned **Ponds** (Python packages), each declaring its parent Ponds in `pond.toml`, and the pipeline follows from those declarations. **Ripples** are the execution units inside a Pond. The **Catchment** (a FastAPI server) is the runtime: it orchestrates, executes, and acts as the catalog.

This file is an internal reference written for quick recall, so it is dense and uses the code's own vocabulary. Don't copy its phrasing into user-facing docs; follow "Writing docs" below instead.

The code is the source of truth for behaviour. `docs/docs/reference/orchestration_theory.md` (the orchestration spec, including the Pond State Variables pseudocode) matches it, and `playground/src/lib/orchestration.ts` is a TypeScript simulation of the same state machine. The Python engine is a behaviour-for-behaviour port of that simulation.

## Positioning

The landing page (`docs/src/components/Landing/index.tsx`) is the source of truth for positioning. In summary:

- Duckstring is a data engineering platform for DuckDB. Package framing ("treat transformations like software packages") is one of its pillars, not the category.
- Audience: anyone keen to try DuckDB as an execution engine who wants a batteries-included engineering framework around it. Write for a working data engineer.
- The four pillars on the landing page: modular transformations (versioned Ponds, SemVer, concurrent major versions), pull orchestration (schedules are set at the end of the pipeline, so nothing runs unless something consumes it), incrementality (Trickles, change sets as Z-sets, changed-key recompute), and code and data managed together (the Catchment is also the catalog). Also: Python-based and CLI-first.
- Tagline: "Get your ducks in a row." It is the hero lead on the landing page.
- Speed claims are acceptable when framed as the platform making good use of DuckDB's speed. This is a loose preference, not a hard rule.
- Analogies are fine when the other domain is broadly known. Avoid ones that need their own explanation.

## Terminology for user-facing text

- **Pull and push.** All Duckstring orchestration is pull: every trigger is set on a terminal node, and paths with no terminal demand never run. Pulse and Tide behave in a push-like way, since their signal effectively travels from the start of the pipeline to the target, but they still skip paths nobody consumes. The code uses "push" and "pull" internally (`has_pull`, the `pond_target` push set). The landing page uses "push orchestration" for conventional start-of-pipeline scheduling, so in docs avoid calling Pulse or Tide "push triggers"; describe what they do instead.
- **Tide.** Lead with "every x", because that is what people search for when they need a daily job. The precise meaning is "keep the target at least as fresh as x" (a staleness bound), and the docs should get to that distinction.
- **Triggers belong on Outlets.** This is a firm recommendation, not a technical requirement: the routes accept a trigger on any deployed Pond.
- **Catalog.** People arriving from other platforms will look for a catalog. The Catchment is the context that holds everything, so it is effectively the catalog: each `name@major` is a schema (`reports_v2`, with bare `reports` resolving to the highest deployed major), tables sit inside it, and lineage, contracts and querying hang off it. Keep the explanation simple.
- **Data format.** Introductory pages can say data is published as Parquet. The default data plane is Iceberg over Parquet files; the detail belongs in guides and reference.

## Writing docs

`docs/documentation_style.md` is the full style guide; read it before writing any page. The main points:

- Firm rules: no em-dashes anywhere (including code comments and table cells); introduce every Duckstring term before relying on it; check every behavioural claim and number against the code.
- The reader is a working data engineer. Don't explain basic data concepts, and don't assume Duckstring vocabulary.
- Keep concept, guide and reference material on separate pages. Concept pages should make sense without code.
- Where a section introduces something, lean towards the order problem, need, concept, mechanism. Keep motivation short.
- Avoid: "X, not Y" framing, "there is no X" statements, stock filler ("crucially", "under the hood"), showy vocabulary ("load-bearing", "first-class", "for free"), aphoristic closing lines, rhetorical questions, reflexive triplets, bold-label bullets as a default, and scattered italics.
- These are mostly soft rules. Don't contort a sentence to satisfy one; clarity for the reader wins.
- Use the demo pipelines as running examples: `transactions`, `products` → `sales` → `reports`, and for incremental topics `orders`, `catalog` → `priced` → `revenue`.

The docs are being rewritten (branch `documentation-rewrite`). The structure is Home, Quickstart, Concepts, Guides and Reference. Guides are task-led and grouped Building Ponds / Incremental Ponds / Changing Ponds / Running Pipelines / Getting Data Out / Operating Catchments; their code examples were run against the real builder, so re-run any you change. Reference covers Orchestration Theory, the Python API, `pond.toml`, the CLI (one page per command group), the HTTP API (auth, conventions and a route table; bodies are left to the Catchment's own `/openapi.json`), Formats and Environment Variables. The public Python docstrings (`core.py` decorators, `Pond`, `Puddle`, `Catchment`; the Trickle builder, `agg`, `acc`, `Delta`) are written to match `docs/docs/reference/python/`, and the CLI's command docstrings and `help=` strings match `docs/docs/reference/cli/`, so change both together. `docs/docs/old/` is excluded from the build. The previous docs are in `docs/docs/old/`; avoid reading them unless absolutely necessary, since their language and structure are being replaced. Base new pages on the code and commit history. When the CLI or API surface changes, update the docs.

## Current state (2026-09)

Built and tested: the freshness runtime, backend and CLI, the web UI (read-mostly Next.js polling the Catchment), fault tolerance, Puddles, concurrent major versions (v0.2.0), both auth models, platform hosting, egress (Spouts), secrets, alerts, metrics, lineage, dbt-mode Ponds, the pluggable data plane, Trickle, and the cloud Duck launchers. The playground is a separate in-memory simulation in `playground/`.

### Concurrent major versions

- The runtime identity of a Pond is the **pond key** `"{name}@{major}"` (`keys.py`). Each deployed major line is an independent live Pond with its own engine node, Duck and storage at `ponds/{name}/m{major}/{registry.duckdb,data/,pond.db}`.
- A Sink wires to the Source major its `[sources]` pin selects. `Pond.read_table` resolves it via `source_majors`, which the Duck computes from the deployed `pond.toml` (absent for puddle runs, which use a flat layout).
- Pond-targeting routes take optional `major` and `version` query params. `major` picks the line (default: highest deployed); `version` must be that line's selected artifact, otherwise 422. `Driver.resolve` handles this. The CLI passes `--major`/`-m` and `--version`/`-v` through everywhere. `/api/status` entries carry `id`/`name`/`major`, and `edges` use ids.
- `min_version` from `[sources]` is enforced at deploy (see Version contract).

### Auth

There are two models.

**Platform auth** (recommended when hosted): the hosting platform gates requests, the UI rides its session cookies, and Ducks dial localhost inside the sandbox. The CLI attaches per-catchment headers set with `catchment connect --header 'Name: value'`, stored in config and merged by `config.auth_headers`.

**Built-in API keys** (bare self-hosting) form an ordered ladder `read ⊂ demand ⊂ full` (`catchment/auth.py`):

- Each route declares its minimum with `dependencies=[auth.read|demand|full]`. read covers status/runs/data/draw/view; demand adds tap/wave/pulse/tide/untrigger and duct-connect; full adds deploy/control/windows/ducts/rotate.
- `audit_routes` runs in `create_app` and fails closed: any unclassified `/api` route (other than `/api/health` and the Duck channel) raises at boot.
- A request's level is the matched key's level. Too low gives 403; missing or invalid gives 401.
- The three keys are stored as sha256 hashes in `catchment_key` (migration `007`). `auth.generate` mints them and prints them once. `init --generate-key` mints the ladder and stores the full key in the registration; `init --key` or `DUCKSTRING_API_KEY` sets a single full key (legacy); with neither, the Catchment is open and `get_principal` returns full.
- Keys can be rerolled without recreating the Catchment: `POST /api/catchment/keys/rotate` (full) or `duckstring catchment rotate-keys [--level]`, which also updates the stored full key.

The **Duck channel** (`/api/duck/*`, `auth.duck`) uses a separate internal token from `auth.ensure_duck_token`. It is persisted in `catchment_meta` so a Duck that outlives a Catchment restart can still authenticate, and it's passed as `--token=<value>` (joined, so a leading `-` isn't parsed as a flag). Rotating user keys therefore never disturbs running Ducks, and no subprocess carries a full key.

The web UI prompts for a key on 401 and keeps it in localStorage. `_http.get/post` take `auth=cfg` (the registration dict). `config.toml` is chmod 0600.

### Hosting

- The static UI can be hosted under a subpath (for example Posit Connect's `/content/{guid}/`): `assetPrefix: "./"` plus an API base derived at runtime in `frontend/src/lib/api.ts`. `tests/test_static.py` and the release workflow guard against absolute asset paths.
- `duckstring.catchment.asgi` is the env-configured ASGI entry for platforms. `DUCKSTRING_ROOT` defaults to `./.duckstring`, which survives restarts but not a redeploy of the bundle. Run exactly one app process.
- `duckstring catchment download [--path DIR=./.duckstring]` pulls the whole root through `GET /api/catchment/archive` (a streamed, uncompressed tar; SQLite is snapshotted with the backup API and `-wal/-shm` files are skipped; DuckDB registries are copied as they are, so download while quiescent), after confirming the size from `GET /api/catchment/usage`. The default path lands inside a deploy bundle, so state survives a platform redeploy.
- The address Ducks dial back to comes from `create_app(root, base_url=...)` (the CLI passes its bind address), then `DUCKSTRING_CATCHMENT_URL`. If neither is set, `SubprocessLauncher` defers spawns (pending keys report `is_running` so liveness checks don't fail them) until a middleware learns the bound address from the first request's ASGI scope (`tests/test_platform.py`).
- `release.yml` builds the frontend and dists on a `v*` tag and publishes via PyPI Trusted Publishing (the `pypi` GitHub environment and the PyPI publisher are configured once).

## Structure

```
src/duckstring/
  core.py                  # Pond/Ripple handles, @ripple/@puddle decorators, Catchment client, pond.toml/entrypoint/import helpers.
                           # Incremental I/O is a capability of the Pond handle; there is no separate @trickle node type.
  dataplane.py             # The data plane: how a Pond publishes and reads tables across Ponds (pluggable, Iceberg by default).
                           # Also hydrate_registry (published state back into a registry: Duck registry-loss recovery and the
                           # DuckFlock routing read-back), the Extension-1 state/ companion snapshots and Extension-2 sidecar stats.
  flock/                   # The Flock: over-envelope compute. A pond.trickle(...) terminal's comprehensive recompute is dispatched to a
                           # serverless engine while the Duck keeps merge/diff/publish. See "The Flock" below.
  iceberg_plane.py         # IcebergDataPlane, the default backend (core deps; DUCKSTRING_DATA_PLANE=parquet opts out)
  iceberg_catalog.py       # FileCatalog: a pyiceberg catalog storing its pointer in a JSON file (avoids SQLAlchemy)
  schema_contract.py       # Version contract: extract_schema(con) + contract_violations(output, contract) (additive check)
  dbt_mode.py              # dbt-mode Ponds: manifest → ripple rows, profiles.yml generation, source materialisation (lazy dbt import)
  trickle/                 # Self-contained incremental engine (Z-sets/DBSP). Imports nothing else from duckstring. Host seam: context.Context.
    context.py             #   Context protocol (con/f/previous_f/read_table/read_delta) + owned constants SYSTEM_PREFIX, NEVER
    io.py                  #   Z-set I/O: append/merge writes, apply_zset, full-row-diff merge_table, read_delta, retention
    builder.py             #   the pond.trickle(...) builder: a DAG of binary incremental joins (any key, any how, bushy)
    agg.py                 #   order-independent aggregate specs for .aggregate()
    acc.py                 #   order-dependent scan specs for .accumulate()
    capture.py             #   plan capture: run a ripple against a recording handle → duckflock_plan:1 JSON (NonCapturable → classic)
    lineage.py             #   column lineage from a captured plan (exact/constant/opaque, never inferred); sqlglot for kind:sql outputs
  trickle_io.py, trickle_builder.py, agg.py, acc.py
                           # compat shims (PEP-562 forwarding) to duckstring.trickle.*, so `from duckstring import agg` still works
  engine/                  # Pure orchestration engine (no FastAPI/DB/HTTP)
    core.py                #   shared dataclasses: NEVER, Window, Pond, Ripple, Trigger, BeginRun, PondState/RippleState
    catchment.py           #   the full engine (Ponds and Ripples, pull and push); the Catchment's brain
    worker.py              #   push-only WorkerEngine used by the Duck to run a Pond Run to completion
    pond.py                #   the per-Pond run ledger (SQLite at ponds/{name}/m{major}/pond.db)
    __init__.py            #   re-exports; tests/test_engine.py is the behaviour gate
  duck/                    # The Duck: per-Pond worker process
    core.py                #   DuckCore: WorkerEngine + ledger + outgoing event buffer (no transport)
    executor.py            #   RippleExecutor (thread pool, ripple loading, parquet export, registry-loss auto-hydration, refresh flag)
                           #   + load_topology (branches to dbt for a dbt-mode Pond)
    dbt_executor.py        #   DbtExecutor for dbt-mode Ponds (runs each model via dbt; no persistent registry connection)
    client.py              #   CatchmentClient (HTTP: poll jobs, post events)
    __main__.py            #   `python -m duckstring.duck ...` serve loop
  catchment/               # The Catchment: FastAPI runtime
    app.py                 #   create_app + lifespan (Driver, scheduler, poller, egress worker, resume_incomplete)
    asgi.py                #   env-configured ASGI entry for platform hosting
    auth.py                #   access-level key ladder, route guards, Duck token
    driver.py              #   Driver: engine + Duck coordination + persistence + trigger/window/spout CRUD + restart restore
    egress_worker.py       #   delivers published output to Spouts
    alert_worker.py        #   drains the alert_delivery outbox → notifier.send
    launcher.py            #   DispatchingLauncher, SubprocessLauncher, NoopLauncher (see Duck launchers)
    fargate_launcher.py    #   FargateLauncher: default remote backend (a Duck per Pond spawn as an ECS/Fargate task)
    ec2_launcher.py        #   Ec2Launcher: alternative remote backend (one EC2 instance per Pond spawn)
    dialback.py            #   RemoteDialback: the reachable Catchment URL for remote Ducks + the shared auto-relay
    relay.py               #   RelayManager: ssh -R reverse tunnel via a small EC2 box, for a local Catchment behind NAT
    db.py                  #   SQLite connect + migration runner
    secrets.py             #   SecretStore (write-only; see Secrets)
    serving.py             #   Serving core: one warm, sandboxed, read-only DuckDB over published data (catchment = catalog,
                           #   pond = schema). Read users get materialised local tables with enable_external_access=false;
                           #   full users get views over all output tables. Wire adapters (pg/Flight) and a Catalog UI are follow-ups.
    schema/001_init.sql    #   database schema (see Catchment database)
    routes/                #   deploy, orchestrate, secrets, alerts, metrics, duck, data, catchment
    registry.py, dag.py    #   pond DuckDB registry paths; inter-pond cycle check
  local/                   # Local pre-deploy testing (no engine/FastAPI/Ducks). See Puddles.
    project.py             #   load_project: pond.toml + entrypoints + puddles/ dirs
    hydrate.py             #   materialise @puddle definitions → puddles/ponds/{source}/data/*.parquet
    runner.py              #   run_pond: one local Pond Run in topological order → puddles/out/
  cli/                     # Typer CLI (`duckstring` / `ds`)
    trigger.py             #   tap/pulse/wave/tide/remove; window add/list/remove (cli/window.py)
    control.py             #   wake/sleep/force/kill/clear/failure-budget
    pond.py, deploy.py     #   pond init/demo/hydrate/run/deploy
    duck.py                #   duck show/set {pond} [--duck catchment|pool|dedicated] [--flock off|upgrade|always] [--engine] [--oom]
                           #   [--instance-type] [--auto-stop] [--clear], and duck pool ls|add|rm. Sizing is by instance type.
    puddle.py              #   puddle ls/show/query (inspect ./puddles via in-memory DuckDB views)
    spout.py               #   spout add/ls/rm {pond}
    secret.py              #   secret set/ls/rm (value prompted, never in argv)
    alert.py               #   alert add/ls/rm/test/log
    status.py, data.py, catchment.py, config.py, window.py, _http.py
  alerts/                  # Failure and freshness notifications
    event.py               #   AlertEvent (rendered and sanitised; never a traceback) + KNOWN_EVENTS/normalise_events
    base.py                #   Notifier protocol + get_notifier(uri) scheme registry + destination validation
    webhook.py             #   WebhookNotifier (http/https), Slack-incoming-webhook-compatible JSON
    email.py               #   EmailNotifier (mailto:), stdlib smtplib
  egress/                  # Publishing a Pond's output to external systems
    credentials.py         #   ${env:NAME} and ${secret:NAME} resolution
    destination.py         #   Spout destination URI parse/validate
    base.py                #   EgressDriver/Capabilities + get_egress(uri) scheme registry
    object_store.py        #   file:// + s3:// + gs:// (snapshot write_full; append-mode mirror)
    postgres.py            #   Postgres CDC sink (apply_delta = delete+insert per transaction, exactly-once watermark)
docs/                      # Docusaurus site → docs.duckstring.com (being rewritten; see Writing docs)
frontend/                  # The Catchment web UI (Next.js static export served at catchment/static). See Web UI.
  src/lib/                 #   api.ts (HTTP client), store.ts (zustand poll store + colour palette), types.ts
  src/components/          #   DagCanvas, Pond/Ripple/TriggerNode, Sidebar, RunHistory, WindowEditor, TraceChart
playground/                # Standalone in-memory simulation (own repo → playground.duckstring.com)
```

### The Flock

`flock/__init__.py` holds the policy ladder (off | upgrade | always), fail-up on OOM, and the row envelope derived from the Duck's memory cap. Engines live under `engines/`; Athena is the default.

DuckDB is the authority on results: dispatch decides where work runs, never what gets published. Three layers enforce this, and `tests/test_flock_conformance.py` is their gate.

1. Each engine owns an allow-list of expressions (`flock/equivalence.py` is the machinery), because equivalence depends on both the expression and the engine running it. Athena allows `+ - *` (Trino shares DuckDB's decimal result rule and decimal arithmetic is exact), `round` (both round half away from zero), abs/floor/ceil/coalesce. It excludes division (DuckDB `7/2` = 3.5 DOUBLE, Trino = 3 INTEGER; `conform` would cast 3 to DOUBLE and publish the right type with the wrong value), CAST (DuckDB rounds .5 to even, Trino rounds half up) and string functions. `tests/test_flock_athena_conformance.py` gates changes to the list: it runs each candidate on both engines (skipped unless Athena is configured) and reports exclusions that turned out to agree.
2. `conform` casts the engine's result to `builder.schema()` (DuckDB's own bound schema) in DuckDB's column order, and rejects a differing column set.
3. A rejected result falls back to local compute and counts as a failed dispatch, so non-conformance shows on /metrics instead of in the data.

`DUCKSTRING_FLOCK_ENGINE` takes a built-in name or a `module:Class` spec. An unimportable spec turns the Flock off rather than failing a run; `tests/flock_fake_engine.py` makes the whole path testable offline. Every dispatch failure falls back to local compute, so the dispatch counters (`take_stats()`, shipped per Ripple Run) are the only sign a Flock is broken.

## Runtime architecture (Catchment and Ducks)

- **The Catchment owns pull.** It runs the full engine (`engine/catchment.py`, modelling Ponds and Ripples, pull and push), holds triggers and windows, and decides when Pond Runs happen. Ripples must be modelled here because results like the Tap-3/1 behaviour and the bottleneck cadence come from ripple-level pull. `start_pond_run` records a `BeginRun(pond, F)` on `state.pending_begin_runs`, and the `Driver` drains and dispatches these.
- **Each executing Pond runs a Duck** (`duck/`, one subprocess per pond key `name@major`). Given `begin_run(F)` it pushes every Ripple to `F` (push-only, `engine/worker.py`), runs the ripple functions, and reports `ripple`/`run_completed` events. It is spawned on the first run and killed when the Pond goes idle (kept warm while a standing trigger is active). It survives Catchment downtime: it finishes in-flight runs from its ledger and engine, buffers events, and replays them idempotently on reconnect.
- There is no cap on concurrent Pond Runs. Completions drive the pull cascade, and that provides the flow control.
- **Transport:** Duck → Catchment is a REST POST (`/api/duck/{name}/{major}/events`); Catchment → Duck is a short poll the Duck holds (`/api/duck/{name}/{major}/jobs`). The Duck always dials back, so the same code works locally and remotely. `DUCKSTRING_CATCHMENT_URL` tells Ducks where to dial. `DUCKSTRING_DISABLE_DUCKS=1` swaps in `NoopLauncher`, so tests can exercise the engine and persistence without spawning processes.
- Cross-Pond data: each major line writes its tables to `ponds/{name}/m{major}/data/{table}.parquet` (atomic tmp+replace), and Sinks read the parquet of the Source major they pin. Each line has its own DuckDB registry at `ponds/{name}/m{major}/registry.duckdb`.

### Duck launchers (see plans/cloud-config.md)

The `Driver` holds one launcher, the **`DispatchingLauncher`** (`launcher.py`). It routes each Pond's spawn by its resolved `duck_target` (from `duck_config`) and by the target pool's provider:

- `catchment` → a `SubprocessLauncher` on the Catchment's own machine (a laptop when local, the host when deployed).
- A pool or `dedicated` → a remote backend chosen by provider. `remotes` is a `{provider: backend}` map with `default_provider="fargate"` (dedicated or missing → Fargate).
  - **`FargateLauncher`** (default): fast serverless containers from the GHCR image, pool cpu/memory as the task size, task-role IAM, data on S3. The command override carries the Duck args and it boots through the remote-boot artifact fetch.
  - **`Ec2Launcher`**: for needs beyond 16 vCPU/120 GB, GPUs, or (eventually) warm pools. One instance per Pond spawn with the pool's `instance_type`; the Duck boots via userdata over the artifact-fetch path; IAM via a worker instance profile. Network placement comes from `DUCKSTRING_EC2_SUBNET`/`_SECURITY_GROUPS`. Without them the instance lands in the VPC's default security group, can't dial back and dies as a silent Duck (it warns once). The AMI's default `python3` must be 3.10 or newer with a matching `pip3`, because the userdata runs `pip3 install <spec>` then `python3 -m duckstring.duck`; stock AL2023 ships 3.9.
- Both remote backends share one `RemoteDialback` (the reachable URL plus the auto-relay), and each registers a drain callback that fires when the URL resolves.
- Built-in S/M/L/XL preset pools (Fargate) always resolve, so `duck = "M"` needs no setup, and the hosted product ships the same names.
- A local Catchment running cloud Ducks uses the same code path as a hosted one; only on-disk state and the `catchment` Duck differ.
- The remote backend is built in `create_app` only when cloud is enabled (a remote data root plus AWS credentials). Otherwise remote targets fall back to local, so a `duck = "heavy"` `pond.toml` runs anywhere.
- A remote Duck dials back to a reachable Catchment URL (`DUCKSTRING_CATCHMENT_PUBLIC_URL`/`remote_base_url`, falling back to the bind address).
- Remote boot output is teed to the serial console (`_CONSOLE_TEE`), and `diagnose` returns its tail (EC2) or the CloudWatch tail (Fargate). A Duck that dies before dialling back has no inbound SSH and no log agent, so this is the only way to see why.
- A Duck's first contact is judged against a provider startup grace (`_STARTUP_GRACE`: catchment 60 s, fargate 3 min, ec2 8 min) instead of the 60 s steady-state silence window, since an EC2 box has to boot an OS before it can dial. Before this, every cold pool's first run failed.
- `manages_processes=True`, and liveness goes through `is_running`. An EC2 record counts as up; the silent-Duck heartbeat catches a dead box, since polling EC2 on every tick is too expensive.
- `DUCKSTRING_DUCK_LAUNCHER=module:Class` replaces the whole launcher.

**The auto-relay** (`catchment/relay.py`) lets a local Catchment behind NAT run cloud Ducks without a manual tunnel. It activates when cloud is enabled, the bind address is loopback or private, `DUCKSTRING_CATCHMENT_PUBLIC_URL` is unset, `DUCKSTRING_RELAY` isn't `off`, and config is present. On the first remote spawn, `RelayManager` provisions a small reachable EC2 box, the laptop holds an outbound `ssh -R` reverse tunnel to it, and Ducks dial the relay, which forwards back. It uses the `Ec2Launcher` pending/drain seam (`start_async` → `set_remote_base_url` drains deferred spawns), terminates itself via a userdata TTL watchdog if the laptop disappears, and is torn down in `shutdown_all`. It only forwards bytes; it holds no state.

Deferred: warm-pool instance reuse across Ponds and the floor/ceiling/idle/keep-warm autoscaler (pool config exists, the pooling scheduler doesn't; v1 launches one instance per spawn), relay security-group scoping and TLS hardening, and real-AWS validation of the EC2/ssh/watchdog paths.

The AWS setup guide (currently `docs/docs/old/guides/cloud.md`) records hard-won operational lessons: the cloud-enable gate, the three IAM roles (including the often-missed `ecs:TagResource` and `iam:PassRole`), security-group asymmetry (workers only dial out, so `sg-duck` has no inbound rules), Fargate and EC2 worker setup, the AMI python3 constraint, the build-your-own-image policy, the Flock's DuckDB-authority rules, and debugging through console output, CloudWatch or SSM instead of opening SSH. Carry these forward when rewriting it.

## Triggers and control (CLI → `/api/ponds/{name}/…` → Driver)

Routes live at `/api/ponds/{name}/…` and accept any deployed Pond (recommending Outlets is a docs convention, not enforced).

**Triggers** (`cli/trigger.py`) are demand signals:
- **tap**: one pull.
- **wave**: a standing pull.
- **pulse**: a one-off push to `now`, propagating upstream.
- **tide**: a standing push with a staleness bound such as `30s` or `1d` (no cron).
- **remove**: drop the standing Wave or Tide; in-flight work drains.

**Control** (`cli/control.py`) acts on a Pond's execution and health (see Fault tolerance):
- **wake** (`engine.wake_pond`): a one-shot, non-propagating pull. It runs once if Sources are already fresher (`sourceF > startF`) without soliciting them. Clears failure/kill.
- **force** (`engine.force_pond`): recompute now at the current freshness even with no upstream change, by resetting the Pond's and Ripples' `endF`. Freshness doesn't advance, so nothing propagates downstream. Clears failure/kill.
- **refresh** (`engine.refresh_pond`, `PondState.refresh_pending`): mark the Pond so its next run is a cold wipe-and-rebuild. The Duck drops the registry (`executor.wipe`) and runs with `previous_f=NEVER`, so Sources are read in full and a Trickle re-bootstraps (changelog re-seeded at `floor=run.f`, prior base and changelog dropped). The raised floor makes downstream consumers miss coverage and reload. It is lazy (it changes how the next run computes, not when), so it propagates at the next genuine freshness. `--clear` removes the flag. See `plans/refresh.md`.
- **repair** (`engine.repair_pond` = force + refresh; `Driver.repair` + `_advance_repair`): rebuild a connected set of Ponds now, sequenced by the Driver in topological order (each node starts once its in-scope parents finish, so it reads their rebuilt output). It steps outside the demand model: the scope is marked `repairing` (`PondState.repairing`, which blocks normal demand in `can_start_pond`, and `derive_blocked` treats a repairing Source as blocking). Connectivity is checked with the "connected through the selection" rule (`driver._connectivity_gap`); `--downstream` or a canvas selection extend the scope. A repaired Pond's floor only advances where freshness genuinely does (an Inlet); the data is rebuilt regardless.
- **sleep** (`engine.sleep_pond`, formerly `stop`): clear push and pull and the Ripples' pull, keeping Ripple push so started runs complete. Cancels the standing trigger. `--upstream` reaches ancestors.
- **kill** (`engine.kill_pond`): terminate the Duck and park the Pond as killed (terminal, overrides retries) until wake/force/clear.
- **clear** (`engine.clear_pond`): reset a failed or killed Pond without running it. It abandons the halted Run (`start_f → end_f`) so liveness doesn't fail it again, and unblocks downstream.
- **failure-budget**: show or set the live retry budgets (`--immediate`, `--on-change`).
- One-shot commands (tap/pulse/wake/force) open the live status view until the target settles (idle/failed/killed/blocked). Standing ones leave it open.

**Bulk operations** (`Driver.batch`, `POST /api/ponds/batch`, `duckstring do`, the UI Selector; see `plans/selector_ui.md`) apply a set of operations to a set of Ponds in precedence order `kill > sleep > reset > wipe > remove > clear > repair > refresh`.
- Repair can't be combined with Remove or Reset. Remove implies Reset and is terminal per Pond (later operations skip a removed line). Reset and Wipe are separate: `wipe`/`wipe_history` clears `pond_run`/`ripple_run` only, without touching data.
- reset, wipe and remove require `confirm` to equal the catchment name (422 otherwise).
- Repair runs once over the live scope; its connectivity check only rejects a gappy manual selection, not disjoint components.
- Per-Pond errors are collected and don't abort the batch.
- The `do` CLI expands a selection client-side (`--all`/`--tree`/`--between`/`--downstream`) and sends an explicit Pond list.
- The UI Selector is a two-phase canvas banner (pick Ponds, then pick operations) reached from **Options → Pond Actions**. The per-Pond Sidebar keeps Force/Wake/Sleep/Kill and Failures; Repair/Refresh/Reset/Remove live in the Selector. The top bar is one `TopBar` (`Logo | Name | Status | ☰ Options`); Collapse-all, Secrets, Alerts and the access level sit in the Options menu.

## Windows (batch availability on Inlets)

Recurrence follows RFC 5545 loosely; there is no cron anywhere (`croniter` was removed). `engine.core.Window(start_anchor, duration, freq_unit ∈ {SECOND,MINUTE,HOUR,DAY,WEEK}, freq_interval, valid_days ⊆ {MON..SUN}|None, until)`. Occurrences are `start_anchor + k·delta`, filtered by `valid_days`/`until`. A Pond is fresh until the active window's end, with `D` = the window duration. `Window.active_end`/`next_boundary` are O(1) and used every tick in `pond_source_f`/`next_wake`; `Window.occurrences` (bounded) is only used to check overlaps when a window is added. Windows are operational config managed with `duckstring trigger window {pond} add|list|remove` (`cli/window.py`), surviving redeploys, and aren't declared in `pond.toml`. `add` needs only `--name` and `--every` (`--start` defaults to 00:00 today; `--duration` defaults to `--every`, giving back-to-back windows).

## Puddles (local pre-deploy testing: `local/`, `cli/puddle.py`)

A **Puddle** is a snapshot of a Source table defined in code, used to test a Pond before deploying it. Definitions live in `src/puddles.py`, either per table (`@puddle("source.table")`) or per Source (`@puddle("source")`). The handle `p` has `con`/`path`/`write_table`/`write_path`/`catchment()` (see `core.py`).

- `duckstring pond hydrate` imports the definitions (registration is a decorator side effect, as with `@ripple`) and materialises `puddles/ponds/{source}/data/{table}.parquet`. That is the catchment-root layout, so `Pond.read_table`'s foreign-read branch works unchanged with `root=puddles/`. Missing definitions are skipped with a warning; `--from-catchment` fills them from the Catchment.
- `duckstring pond run [--ripple X] [--fresh]` is one local Pond Run (sequential topological order; no engine, freshness or Ducks). A full run resets `puddles/out/` (registry and exported parquet). A self-puddle (`puddles/ponds/{this_pond}/`) is copied in first as the seed, which makes incremental reruns idempotent.
- `duckstring puddle ls|show|query` inspects `./puddles` through in-memory DuckDB views (`"{pond}"."{table}"`; output overrides a self-puddle of the same name).
- Entrypoints can be declared in `pond.toml` (`[pond] ripples`/`puddles`, defaulting to `src/pond.py`/`src/puddles.py`); deploy and the Duck executor honour them via `core.import_pond_module`.

Tests: `tests/test_puddle.py`. The `sales` demo includes a worked `src/puddles.py`.

## dbt-mode Ponds (`dbt_mode.py`, `duck/dbt_executor.py`; see `plans/dbt.md`)

A dbt project can be deployed as a Pond with no `@ripple` code. Each dbt model becomes a Ripple (with freshness, failure and retry tracking), and dbt's `ref()` graph becomes the Ripple graph. It needs the `duckstring[dbt]` extra (dbt-core + dbt-duckdb); `dbt_mode.py` imports dbt lazily everywhere so the rest of duckstring runs without it. A Pond is dbt-mode when `pond.toml [pond] dbt_project = "dbt/"` is set. That excludes `@ripple` code in the same Pond, but Python Ponds can sit upstream or downstream.

- **Translator** (`dbt_mode.py`): `parse_manifest` runs `dbt parse` (no SQL executed) to get a Manifest. `manifest_to_ripples` emits the same row shape `@ripple` discovery produces: `{"func": model_name, "name": model_name, "parents": [parent_model_names], "always_run": False}`. `func` is the model name as a string and is only used as a dict key to resolve parents to names, so `routes/deploy._register` and `_incoming_topology` store it unchanged with no schema change. `write_profile` generates `profiles.yml` (profile name `duckstring`) pointing dbt-duckdb at the Pond's registry.
- **Deploy** (`routes/deploy.py`): `_pond_config` reads `dbt_project`, and `_discover_ripples` branches to `_discover_dbt_ripples` (parse with a temporary `:memory:` profile, then translate). A parse failure or missing `dbt_project.yml` returns 422.
- **Runtime** (`duck/`): `executor.load_topology` branches to `_dbt_topology`, and the Duck uses `DbtExecutor`. It holds no persistent registry connection, because dbt opens its own during a run and two connections to one DuckDB file conflict. Each step (source materialisation, dbt run, export, wipe) opens a short-lived connection, serialised by one `_lock`. `submit(model)` runs `dbt run --select model` against the registry (with `--target-path` isolated per major line). `export` publishes the model tables in the `main` schema through the data plane, contract-gated like any Pond; materialised Sources live in their own schemas and are never republished.
- **Cross-Pond sources**: a dbt `source('X','tbl')` maps to the Duckstring Source Pond `X`, which must be listed in `pond.toml [sources]`. Before a model runs, `materialize_sources` reads each such Source with `Pond.read_table` and writes it into the registry as exactly the relation dbt resolves (`{schema}.{identifier}`, from the manifest). A source that doesn't match a declared Source is left to dbt. A `MissingSourceAsset` makes the Duck park the model as waiting, as for any Ripple.
- **Scope (v1)**: models are plain overwrite nodes and don't use Trickle (dbt manages its own incremental materialisation). dbt-duckdb only.

Demo: `duckstring pond demo --dbt` scaffolds `shop_orders` (a plain `@ripple` Inlet) and `shop_analytics` (dbt-mode, three models `orders_clean → revenue_by_product → top_products`). Test: `test_dbt_mode_pond_chain_runs_end_to_end` (deployed-Duck e2e, `pytest.importorskip` on the extra).

## Egress (Spouts: `egress/`, `cli/spout.py`; see `plans/egress.md`)

Egress gets a Pond's output into systems a team already runs (object stores, Postgres) through pluggable drivers.

A **Spout** is a Pond's egress binding `(pond, major, table|*, destination, mode)`. It's operational config (CLI/API, persisted, survives redeploys), not part of `pond.toml`, because destinations and credentials depend on the environment. The destination is a URI whose scheme selects the driver (`file`/`s3`/`gs`/`postgres`). Credentials are `${env:NAME}` or `${secret:NAME}` references resolved only at egress time (`egress/credentials.py` `resolve`; `${env:}` reads the Catchment's environment, `${secret:}` reads the secret store). `egress/destination.py` validates the scheme, credential syntax and mode (`auto`/`full`/`append`) without resolving anything. CRUD goes `Driver.add_spout`/`list_spouts`/`remove_spout`/`resync_spout` → `/api/ponds/{name}/spouts` (+ `/resync`, full) → `duckstring spout add|ls|rm|resync {pond}`. A Spout's name defaults to the table (or the scheme for all tables), with `-2`/`-3` added on collision.

**Driver seam** (`egress/base.py`): the `EgressDriver` protocol (`capabilities`/`ensure`/`write_full`/`apply_delta`/`test_connection`), `Capabilities(supports_delta, supports_delete, transactional)`, and `get_egress(uri)`. A known scheme with no driver yet raises "not implemented yet", and the worker parks the Spout.
- `test_connection(con)` backs the UI's Test button and writes no data: file:// writes and deletes a probe, postgres ATTACHes and runs `SELECT 1`, s3/gs configure httpfs and list the prefix. Errors are sanitised so they never contain a credential. Exposed at `POST /api/ponds/{name}/spouts/test` (full), which returns `{ok}` or `{ok:false,error}`; a connection problem is a 200 result, not a 5xx.
- **Object store** (`egress/object_store.py`): snapshot `write_full` to `{prefix}/{table}.parquet` with `supports_delta=False`, so the worker always writes in full. `file://` writes locally (atomic tmp+replace). `s3://`/`gs://` write via DuckDB `httpfs` and the secret manager, with credentials from the URI query (`?key_id=${env:..}&secret=${env:..}&region=..`) or the AWS credential chain for `s3://` with no key; `gs://` requires HMAC. The secret `CREATE` error is masked so it can't echo a credential, and the target URI carries no query so errors can't leak one either. `mode=append` uses `ObjectStoreEgressDriver.mirror`: the table's published collection (parts, tiers, sidecar, Extension-1 `state/`) is reconciled by part name on each delivery, O(new parts), and the destination remains a readable Duckstring layout. `full`/`auto` keep the snapshot behaviour.
- **Postgres** (`egress/postgres.py`, `postgres://`, capabilities delta + delete + transactional) is the main incremental sink, since a merge Trickle's changelog is a CDC stream. It uses the DuckDB `postgres` extension (ATTACH plus plain DuckDB SQL; no SQLAlchemy or psycopg). `apply_delta` deletes the changed and removed keys and re-inserts the present rows in one transaction (the same net effect as `INSERT … ON CONFLICT`, but portable). The watermark lives in the destination (`_duckstring_egress`, written in the same transaction), giving exactly-once delivery across crashes. Only a merge Trickle has a primary key, so the transactional-PK requirement is checked at `add_spout` (`Driver._assert_transactional_pk`, on a published table) and again at egress.

**A Spout is a real Pond**, the egress counterpart of a Pond Draw (migration `012`). `_create_spout` mirrors `_create_draw`: a `pond_name` row with kind `outlet` named `{source}#{spout}`, a synthetic `pond_version` with one `'egress'` ripple, and `pond.is_spout=1`, wired to its source through `pond_to_pond`. It runs as an engine node with a **standing Wake** (`PondState.standing_wake` + `Pond.is_spout`; engine `tick` re-arms a non-propagating pull whenever it's idle). It delivers when `sourceF > deliveredF`, never solicits its source, and being terminal never blocks anything.
- The egress worker plays the part of the Spout's Duck. The engine sends the Spout's `BeginRun` to `_pending_egress` instead of a Duck (the `is_draw` branch in `_dispatch_begin_run` is the template, and liveness skips it). The worker drains `take_spout_jobs`, delivers, and reports through `complete_spout_run`/`fail_spout_run`, the same `pond_run`/`ripple_run` path a Pond uses. A Spout therefore gets run history, tracebacks and `/api/runs` without extra code.
- Control verbs apply to Spouts; demand verbs don't. `Driver.spout_wake/force/sleep/kill/clear` reuse `clear_pond`/`kill_pond` plus a persisted `armed` flag (Sleep/Kill disarm, Wake/Force re-arm, Force re-delivers from scratch) → `POST /api/ponds/{name}/spouts/{spout}/{action}` (full) → `duckstring spout wake|force|sleep|kill|clear|resync`.
- A Spout appears in `ponds[]` with `is_spout` (and its source → spout edge in `edges`), drawn dashed in the UI like a Draw. `pond_spout` (keyed on the Spout's `pond_id`) holds only `table_name`/`destination`/`mode`/`armed`; fault and retry state are the node's `pond_state`/`pond_retry`.
- **Windows throttle delivery** the same way they work on an Inlet. `pond_source_f` returns the active window's end for a windowed Spout, so the Wake fires once per window (waiting through a gap or until the source publishes). The run is stamped with the window end, and the worker ships the source's data at the source's real freshness (the job's `source_f`, which the CDC watermark follows, exactly-once). Because a Spout is a real Pond, windows reuse the `pond_window` CRUD unchanged (`trigger window {source}#{spout}`, or the UI `WindowEditor` on the Spout's key), and `next_wake` also wakes at a windowed Spout's boundaries. The old `--every` schedule was removed because a staleness bound would solicit the source; keep the source fresh with a Tide or Wave and throttle the Spout with a window.

**The worker** (`catchment/egress_worker.py`) is an async loop in the Catchment process, woken on run completion or control actions (`Driver._signal_egress`) and on a 5 s tick. For each job from `Driver.take_spout_jobs`, a transactional delta-capable driver reads the changelog delta over `(in-destination watermark, f]` (`trickle_io.read_delta`) and calls `apply_delta`, falling back to `write_full` on a full read (bootstrap, coverage miss, or a changed overwrite source). Other drivers snapshot. Success calls `complete_spout_run`; failure calls `fail_spout_run(... traceback)`, which never fails the source.

**UI**: `PondNode` draws a Spout dashed with `[SPOUT]`. The Sidebar's `SpoutPanel` holds its control verbs and throttle `WindowEditor`. A source Pond's `SpoutEditor` lists and adds Spouts. Credentials are entered as names, either an env var or a stored secret (a datalist of stored secret names), and assembled into `${env:NAME}`/`${secret:NAME}`, so a value never crosses the wire. The add form's Test button calls `POST .../spouts/test`, and the result is tagged with its destination so editing clears it. The access badge shows three capabilities (`Manage | Demand | Read`, green ✓ when granted, grey – when not; labels stay white).

Still to do: real-backend write e2es (MinIO/moto S3, containerised Postgres) in CI. Locally the Postgres logic is tested against a DuckDB-attached destination, which uses the same SQL. Tests: `tests/test_spout.py`, `tests/test_egress_credentials.py`, `tests/test_egress_file.py` (with a real-Duck e2e), `tests/test_engine.py` (standing Wake and windowed Spouts).

## Secrets (`catchment/secrets.py`, `routes/secrets.py`, `cli/secret.py`)

A write-only, catchment-wide store for credentials referenced as `${secret:NAME}`.
- `SecretStore(root)` keeps a plaintext `secrets.json` at the catchment root (chmod 0600, atomic tmp+replace; names match `[A-Za-z_][A-Za-z0-9_]*`) with `set`/`names`/`get`/`remove`. `names()` returns `[{name, set_at}]` and never values. `get()` is internal only; there is no endpoint that reads a value back.
- `create_app` puts the store on `app.state.secret_store` and calls `credentials.set_secret_provider(store.get)`, a module-level provider so `${secret:}` resolves deep inside a driver.
- API (`/api/secrets`, all full): `GET` (names only), `POST {name,value}` (422 on a bad name), `DELETE /{name}`.
- CLI: `duckstring secret set NAME` prompts for the value hidden (never in argv); `secret ls`; `secret rm NAME`.
- UI: `SecretsMenu` in the Options menu (full access only) lists names and has a write-only set form. The Spout add form offers those names in a datalist.
- Encryption at rest is deliberately skipped (plaintext, 0600); this is the author's decision. `set` does send the value in the POST body by design, so use HTTPS.
- The store is excluded from the catchment archive/download (`routes/catchment.py` `_SKIP_NAMES`), so secrets never travel in a state bundle.

Tests: `tests/test_secrets.py`.

## Lineage (`trickle/lineage.py`, `cli/lineage.py`, migrations `019`/`020`; see `plans/lineage.md`)

Lineage is recorded, never inferred: a lineage fact is either exact or absent. It is observability only. It holds no engine state, and a failure to write or emit lineage never fails a run. There are four levels.

- **Pond level**: the declared graph (`pond_to_pond`, `/api/status` edges, the recursive `/api/view`).
- **Table level (observed per run)**: the Pond handle records every read and write it brokers (`Pond._record_read`/`record_lineage_write`/`take_lineage`). Builder terminals report through an optional `record_lineage_write` hook on the Context, keeping `trickle/` dependency-free; the dbt executor reports materialised sources and manifest parents. The executor drains lineage per Ripple Run (`on_done(..., lineage)`), the Duck sends it on the `ripple` event, and `Driver._record_lineage` stores it in `ripple_run_lineage` (migration `019`). It uses `''` instead of NULL for own/source-less rows so replays can't duplicate, and it's purged wherever run history is. Served by `GET /api/lineage?pond=&major=&table=` (read), `duckstring lineage`, and the Sidebar's "Lineage · observed" section.
- **Column level (static per version)**: `trickle.lineage.column_lineage(body)` walks a captured plan. Equi-join keys unify provenance across both sides (left side only for semi/anti), pipeline operations apply in call order (filters add no edges), agg/acc metrics map to their input columns (plus the `.along` axis), and chained references to the Pond's own outputs resolve transitively. A constant gives an empty set; anything unprovable gives None (opaque). Explicit `alias.col` references don't need source schemas; only enumeration (star or bare names) does. Captured at deploy by `routes/deploy._capture_column_lineage` (best effort per Ripple, never fails a deploy; schemas from `pond_version_schema`) into `pond_version_column_lineage` (migration `020`, recomputed on each deploy). Exposed with `?columns=true` / `--columns`. `kind: sql` outputs are resolved with sqlglot (the `duckstring[lineage]` extra, present in dev and CI) by parsing and qualifying against the composed input's schema; if it's missing or fails, the output is opaque. This also provides the observed-use half of the pinned-minor contract question (impact analysis = column lineage × the contract).
- **Row level (temporal provenance)**: `GET /api/ponds/{name}/trace?table=&where=` (`duckstring trace pond.table --where …`) finds the newest `_duckstring_f` among matching published rows, then `Driver.trace_run` returns the producing run (version, timings, status), its input window `(previous_f, f]`, and the declared Sources. The predicate runs at the `/api/query` trust level over the exported snapshot.

**OpenLineage emission**: on `run_completed`, `Driver._emit_openlineage` builds a standard RunEvent (deterministic `uuid5(catchment_id:pond:f)` runId; inputs are the observed reads at that `f`; outputs and schema facets come from the captured contract) and enqueues it through `_emit_alert("openlineage", …)`. The alert outbox handles delivery, retries and auditing, and its per-`f` dedup absorbs replays. `WebhookNotifier` posts the RunEvent as is, without the Slack `text` wrapper. The `openlineage` kind is in `alerts/event.EXPLICIT_EVENTS`: it can be subscribed to explicitly but isn't included in `all`, so ops channels never receive raw catalog events, and it costs nothing without a subscriber.

Deferred: dbt model column lineage (needs compiled SQL at deploy), cross-Catchment column lineage, lineage-row retention (will follow `pond_run`'s retention policy). Tests: `tests/test_lineage.py`, `tests/test_column_lineage.py`.

## Alerts (`alerts/`, `catchment/alert_worker.py`, `routes/alerts.py`, `cli/alert.py`; see `plans/alerts.md`)

Failure and freshness notifications. They're shaped like Spouts (operational config outside `pond.toml`, a driver chosen by URI scheme, `${env:}`/`${secret:}` credentials from egress, and an async worker that never feeds a failure back into the engine). A channel isn't an engine node, since a notification has no freshness or run semantics. It's config plus an outbox plus a worker, and alerting only observes state transitions the engine already computes. Migration `014_alert.sql` (`alert_channel` + `alert_delivery`).

- **Notifier seam** (`alerts/base.py`): the `Notifier` protocol (`send`/`test`) and `get_notifier(uri)` (mirroring `get_egress`); `parse_notifier_destination` validates the scheme and `${…}` syntax without resolving. `WebhookNotifier` (`http`/`https`) POSTs a body that is both a structured event and Slack-incoming-webhook compatible (a top-level `text` summary plus the structured fields), which covers Slack, generic receivers and PagerDuty through a proxy. `EmailNotifier` (`mailto:`) uses stdlib `smtplib`, with SMTP host/port/user/pass/from from the URI query or `DUCKSTRING_SMTP_*`. An `AlertEvent` (`alerts/event.py`) is the rendered, sanitised payload: it carries the error message and never a traceback, because a channel is a third-party surface (the same reasoning as `_redact_tracebacks`).
- **Event-driven firing** (from `Driver` state transitions): `failure` (a Pond Run gives up, a dead or silent Duck, or a Duck-level error), `contract` (the Duck refused to publish because of an additive-contract break), `spout` (a Spout delivery failed, scoped to its source Pond's name), and `recovery` (a failed Pond or Spout clears).
- **Tick-driven firing** (in `scheduler_tick` next to `_check_liveness`): `freshness` (a scoped Pond's staleness exceeds the channel's `--stale` bound) and its `recovery`.
- **Root-cause dedup**: failure alerts fire only for root causes. A Pond blocked by an upstream failure is `is_blocked`, never `is_failed`, and the fail path only runs on the root, so blocked Ponds don't alert; the failure payload lists the currently blocked Ponds as the blast radius. Within a root, the outbox constraint `UNIQUE(channel_id, dedup_key)` with key `"{kind}:{pond}:{f}"` makes each episode fire once (retries at the same failed `f` give one alert; a new failed `f` gives a new one). Recovery is emitted centrally in `_process` by diffing `_alerted_failures`, so every path that clears a failure (a fresher run, wake/force/clear, redeploy) is covered exactly once. A kill is intentional and doesn't produce a recovery. A `recovery` also reaches channels subscribed only to the originating kind (via `match_kinds`), so subscribing to `failure` includes its recovery.
- **Delivery** (`alert_worker.py`, the same shape as the egress worker): woken by `Driver._signal_alert` or a 5 s tick, it drains `take_alert_deliveries` and calls `Notifier.send` in a thread pool with a per-send timeout. Each delivery is marked `sent`, or has `attempts` bumped until it's parked as `failed` at `MAX_ATTEMPTS` (a dead channel stops retrying but stays auditable). `Driver._emit_alert` is the enqueue point: it matches enabled channels by scope and event filter and `INSERT OR IGNORE`s the dedup-fenced rows. It's wrapped so a bug in alerting can never break a Pond Run.
- **Channel config**: `(name, destination, scope_name|NULL, scope_major|NULL, events CSV|'all', stale_ms?)`. Like every Pond-attached construct, a Pond-scoped channel targets a specific `name@major`; both NULL means catchment-wide. On the wire, CLI and UI the scope is a single string, `"name@major"`, `"name"`, or none (`Driver._split_scope`). A bare `"name"` resolves to the Pond's highest deployed major when the channel is added; an explicit `name@major` can precede deployment, but an undeployed bare name is rejected. Matching (`_emit_alert`/`_check_freshness`) compares `(scope_name, scope_major)` exactly with the event's Pond; catchment-wide channels match everything. Removing `name@major` deletes channels scoped to exactly that line (`plans/remove-pond.md`).
- **Surface**: CLI `duckstring alert add --to <uri> [--pond N [--major M]] [--on failure,…|all] [--stale 1h]` and `alert ls|rm|test|log`. API `/api/alerts` (all full, since a destination is an outbound surface): `GET`/`POST`, `DELETE /{name}`, `POST /{name}/test` (a connection problem is a 200 `{ok:false,error}`), `GET /alerts/deliveries` (the audit log). UI (full only): an `AlertsMenu` next to Secrets (channels with test/remove, an add form with event-kind chips and a freshness-SLA input, and a delivery-log tab), and a per-Pond Alerts section in the Sidebar (`AlertEditor`, fixed to the Pond's `name@major` via `fixedScope`). Both reuse `AlertChannelForm`/`ChannelRow` and the `api.ts` functions `fetchAlerts/addAlert/removeAlert/testAlert/fetchDeliveries`.
- **Re-notify** (opt-in): `alert_channel.renotify_ms` (migration `017`; `--renotify 6h`, API `renotify_ms`, or the UI form). While a failure or freshness episode lasts, a re-notifying channel's dedup key gains a time bucket (`:r{bucket}` in `_emit_alert`), so it fires once per interval instead of once per episode. `_check_renotify` (scheduler tick) re-emits ongoing failures and `_check_freshness` re-emits still-stale Ponds; channels without re-notify swallow the repeats at the per-episode dedup. Recovery still fires once per episode, and a killed Pond is never re-notified. Deferred: acknowledgement and escalation (leave to PagerDuty).

Tests: `tests/test_alerts.py`.

### Metrics

`GET /metrics` is a Prometheus scrape endpoint (`routes/metrics.py`, rendered by `render_metrics` from `Driver.metrics_snapshot()`, hand-written text exposition with no new dependency). It's mounted at the root rather than under `/api` and is unauthenticated, following exporter convention, so it sits outside the `/api` audit and before the static `/` catch-all. Families (`duckstring_*`):

- `up`
- `pond_freshness_lag_seconds` (the headline `now − end_f`)
- `pond_failed`/`pond_blocked`/`pond_killed` (0/1)
- `pond_runs_completed_total`/`pond_failures_total` (counters rebuilt from the DB, so they stay monotonic)
- `spout_delivery_lag_seconds`/`spout_failed`
- `alert_deliveries_total{status}`
- `flock_dispatched_total`/`flock_dispatch_failures_total` (per Pond, accumulated from the per-Ripple-Run deltas Ducks report on the `ripple` event via `flock.take_stats()`). A snapshot at run completion would be wrong, because a Duck killed mid-run is respawned and resumes, so the process that dispatched often isn't the one that finishes. A failed dispatch still runs locally, so the failure counter is the only sign the Flock is degrading; the last error also appears on `/api/status` as `flock_error`.

Pond names are labels, so restrict network access if they're sensitive. Tests: `tests/test_metrics.py`.

## Data plane (`dataplane.py`, `iceberg_plane.py`)

The data plane is how a Pond publishes its tables for other Ponds and how it reads theirs. It's separate from the DuckDB registry where Ripples compute; Ripples always compute on the registry, and the data plane only handles export and interchange. It's pluggable behind `dataplane.get_data_plane()`, selected by `DUCKSTRING_DATA_PLANE` (default `iceberg`).

**`IcebergDataPlane`** (default; pyiceberg is a core dependency, SQLAlchemy is not): an Apache Iceberg metadata and snapshot layer over Parquet data files.
- The catalog is our own `FileCatalog` (`iceberg_catalog.py`), a `MetastoreCatalog` subclass that stores the `table → metadata.json` pointer in a JSON file. pyiceberg's only embedded catalog is `SqlCatalog`, which needs SQLAlchemy just for that pointer, so we replace the pointer store and inherit the rest.
- One catalog per `name@major` at `{data_dir}/catalog.json` (namespace `pond`), which keeps major lines physically isolated and avoids contention between Ducks. There's a single writer per line, so commits need no optimistic-concurrency check; saves are atomic (`os.replace`) for cross-Pond readers.
- Writes are pyiceberg Arrow `overwrite`, one commit per Pond Run, with the snapshot stamped `duckstring.f=<iso>`. Reads use DuckDB's `iceberg` extension (`iceberg_scan` on the snapshot metadata; `prepare(con)` loads it, which downloads once, so an offline Catchment should use `parquet`).
- A flat `{table}.parquet` sidecar is written with each commit so ducts, draws, direct file serving and the fallback for Sources not yet on Iceberg behave as before. The catchment archive includes `catalog.json` and the Iceberg directories through its root walk.

**`ParquetDataPlane`** (`DUCKSTRING_DATA_PLANE=parquet`, the lightest and offline option):
- A plain overwrite output is one `{table}.parquet` in the line's `data/` directory, overwritten each run (atomic tmp+replace).
- An append-only Trickle table (append history, `__changelog`, warm `__band`, `__droplog`) is a directory of per-run parts `{table}/{f}.parquet`, one `_duckstring_f`-homogeneous file per run (warm bands one per fold), so each run writes only its delta. `_export_parts` reconciles parts with the registry's freshness set (writing missing parts, pruning ones dropped by retention) and writes a 0-row marker part at the run's `f` for an empty (bootstrap-only) changelog so it reads as an empty delta rather than a coverage miss.
- A merge main is tiered: its cold base is written as freshness-ordered chunks in `{table}__base/` (`_publish_tiered_main`/`_export_bands`; see below).
- `read_select`/`list_tables`/`table_path` handle directories (`read_parquet('{table}/*.parquet')`). Part names are canonical UTC ISO via `trickle.part_name`/`part_f`.

**Interface details**: writes carry a `mode` (`overwrite` today; `append`/`merge` are reserved and raise) and a per-run `f` stamp, and the `_duckstring_*` column namespace is reserved (rejected at publish). As-of reads by `f` are wired in (`read_select(..., as_of=)` resolves the snapshot whose stamped `f <= as_of`); the default is latest. The data plane is used by executor export, the local runner's export and seed, `Pond.read_table` foreign reads, and `/api/data`. Duct and draw transfers (`poller.py`/`draw.py`) use the flat layer: a wholesale table ships its single file, and an append-only table ships only the parts newer than the consumer's `landed_after`, which the consumer drops into its own parts directory (parts are immutable and idempotent by name, so no merge).

**The merge main is a three-tier log-structured store** (`plans/trickle-main-incremental.md`):
- **L0**: the hot `__changelog` (per-run parts; the source for `read_delta` windows).
- **L1**: warm Z-set bands consolidated over freshness ranges (`{table}__band/`), written by `fold_warm` when the hot tier grows past twice the chunk threshold. It always leaves a hot window so caught-up consumers still get a delta, which raises the delta floor.
- **L2**: the cold clean base (strictly `d=+1`, one row per PK), published as size-bounded, freshness-ordered chunks in `{table}__base/` (DuckDB `FILE_SIZE_BYTES`). It's rewritten only at a cold compaction (`checkpoint`, k=1: when warm ≥ cold, and never below `DUCKSTRING_COMPACT_THRESHOLD`, default 256 MiB, or a per-table override recorded at the merge write).
- Per-run publishing is O(change); the O(base) cold rewrite is amortised by k=1.
- Reading a merge main reconstructs the current state as latest-per-PK over the cold base plus warm and hot rows filtered to `> f_base`. The cold base is anti-joined by the retraction keys, never grouped (`DataPlane.read_select` over each plane's `_raw_read_select`). The Iceberg plane serves every tier from the flat layer and never commits them to the catalog.
- The cold base ships only when its `f_base` has advanced past the consumer's `base_after` (the draw gate), so the large base isn't resent on every draw. Warm bands travel with the incremental parts, and the consumer reclaims tiers made redundant by an advanced `f_base`.

**Object-store tests**: `tests/test_object_store.py` runs the data plane against a real S3 API, moto by default (fast, no Docker) or an external endpoint via `DUCKSTRING_TEST_S3`. CI starts MinIO, which rejects unsigned requests (moto doesn't), so it also covers credential bugs. An S3-compatible endpoint override (`?endpoint=` or `DUCKSTRING_S3_ENDPOINT`) is wired through both halves of the plane, fsspec's `client_kwargs.endpoint_url` and DuckDB's secret (`ENDPOINT`/`USE_SSL`/`URL_STYLE 'path'`), which is what makes MinIO, Ceph or R2 usable as a data root. `test_runtime`'s broad e2e suite is pinned to `parquet` (fast and offline); the Iceberg e2es are `test_demo_chain_runs_on_iceberg_end_to_end` and `test_iceberg.py`. See `plans/data-plane-iceberg.md`.

## Trickle: incremental I/O with Z-sets and DBSP joins (`duckstring/trickle/`; see `plans/trickle-dbsp.md`, `plans/trickle-dag.md`)

### Packaging

`duckstring/trickle/` (`context.py`, `io.py`, `builder.py`, `agg.py`, `acc.py`, …) is a standalone DBSP-style incremental engine over DuckDB that imports nothing else from duckstring. Its only connection to the host is `context.Context` (a DuckDB connection, a stable epoch `f`/`previous_f`, and `read_table`/`read_delta`), which `Pond` satisfies structurally. It owns `SYSTEM_PREFIX` and `NEVER`; the data plane re-exports `RESERVED_PREFIX` from it, so the dependency always points from duckstring to trickle. This keeps it ready to be split into its own pip package if a second consumer appears. The top-level modules `trickle_io.py`, `trickle_builder.py`, `agg.py` and `acc.py` are PEP-562 forwarding shims that keep existing public imports working; on extraction they become the adapters to repoint. **Never import from the rest of duckstring inside `trickle/`**; inject host concerns through `Context` or a function argument (as `read_delta(..., dp=...)` does for the data plane).

### Model

A Trickle is an incremental use of a Ripple: it keeps history so a consumer can build its output from only the rows that changed in the window `(pond.previous_f, pond.f]`. Every change is a **Z-set**, a relation with an integer weight `_duckstring_d` per row (`+1` present, `-1` retracted). An update is a `-1` of the old full row plus a `+1` of the new one, so deletions and updates carry full row images. That's what lets the builder join on any key and propagate deletes correctly. The benefit is incremental I/O plus incremental joins (a join doesn't recompute its whole output when only some sources change).

Incremental behaviour is a capability of any `@ripple`, reached by writing history-preserving tables (`append_table`/`merge_table`) and reading deltas; there's no separate decorator. The mode is chosen per table at the write call, and a merge's primary key is declared at the write call too. The I/O lives in `duckstring/trickle/io.py`, reached through `pond` methods or the `duckstring.trickle` package. (This replaced an older upsert/tombstone model with `_duckstring_op`/`_duckstring_hash`, key-set helpers and an FK=PK builder constraint, described in `plans/trickle.md`. That version was never released, so the format changed with no migration.)

### Storage and system columns

- Reserved columns: `_duckstring_f` is the freshness stamp on every history and changelog row, and on each merge base row (its last-write freshness, used for as-of reads and the data viewer); `read_table` strips it. `_duckstring_d` is the Z-set weight on a merge changelog.
- A merge main is the three-tier store described under Data plane: a clean cold base folded up to `f_base` (chunked, written only at cold compaction), warm Z-set bands (`__band/`, `fold_warm`), and the per-run `__changelog` (hot). The current state is reconstructed on read (latest-per-PK over cold, warm and hot filtered `> f_base`, with cold anti-joined by retraction keys; see `trickle/io.py:reconstruct_sql`/`_clog_union_sql`).
- `_duckstring_f` is always read as a content predicate (`WHERE _duckstring_f > previous_f AND _duckstring_f <= f`), never as a snapshot cursor.

### Write API (on the Pond handle)

- `append_table(name, rel, *, pk=None, fail_on_conflict=True)`: insert-only history, all `+1`, idempotent at `f`. `pk` is optional metadata. With `pk` set and `fail_on_conflict=True` (default), uniqueness is checked across the batch and existing history, raising before writing. `False` skips the check (and it's a no-op without `pk`).
- `merge_table(name, rel, *, pk)`: `pk` is required and `rel` is the complete current state. It's diffed against the reconstructed prior state strictly before `f` (`reconstruct_current(con, name, before=f)`) as a full-row Z-set (`new(+1) ⊎ prior(-1)`, consolidated), and the diff is appended to the `__changelog`; the main isn't written.
- `apply_zset(name, zset, *, pk)`: the low-level primitive the builder's incremental path uses. It appends a Z-set directly to the changelog, consolidating once (full-row `GROUP BY`, BIGINT weight) and then committing the changelog `DELETE(@f)+INSERT` in one transaction. The main is never touched per run.
- Diffing against the pre-`f` state and replacing the `f` window means a re-merge at the same `f` and a crash replay are both correct. An empty consolidated change leaves the changelog alone.
- `fold_warm(con, name, target_f)` moves the hot slice `(f_warm, target]` into a consolidated warm band (collapsing a→b→c→d, raising the delta floor). `checkpoint(con, name, target_f)` folds cold, warm and hot up to `target` into a fresh clean base, clears the warm tier, and advances `f_base`/`f_warm`. It needs no lock, because reads are latest-per-PK and swapping the base is idempotent even if checkpoints overlap.

### Read API

`pond.read_delta("src.table")` returns a `Delta` over the window, with `.zset` (user columns plus `_duckstring_d`), `.is_full`, and the conveniences `.upserts`/`.deletes`.

| Source | Delta |
|---|---|
| Append | the history window, all `+1` |
| Merge | the changelog window, consolidated by full row (multiple updates or a delete and re-add collapse) |
| Plain overwrite Ripple | a full read (`is_full=True`) if its published `f` moved past `previous_f`, otherwise empty (an unchanged Ripple is a stable operand) |
| Bootstrap (`previous_f=NEVER`) or coverage miss | a full read |

`read_table` on a Trickle source returns the clean current state with system columns stripped. Coverage depends on the published `floor` (in the `_trickle.json` sidecar): a delta is valid only when `previous_f >= floor`. The floor is set at bootstrap or refresh (`= run.f`, with an empty changelog) and raised by retention.

### Builder (`trickle/builder.py`)

`pond.trickle(spine).join(pond.trickle(dim), on=…, how="inner").filter(…).mutate(col=…).select(…).merge(name, pk=…)`

- The builder records a DAG of `_Source`/`_Join` nodes (a DAG of binary incremental joins; see `plans/trickle-dag.md`). `on` is any equi-join key: shared column names, or a `{left: right}` dict, with `alias.col` to disambiguate. `how` is inner/left/right/full/semi/anti (`_JOIN_SQL`; `_LEFT_ONLY` = semi/anti), all maintained incrementally, including the NULL-padded rows of outer joins. No input is privileged. A join's operand may itself be a join DAG, so bushy `(a⋈b)⋈(c⋈d)` and snowflake shapes work directly. An operand carrying its own `.filter()`/`.mutate()`/`.select()`/`.aggregate()`/`.sql()` raises; attach those to the result.
- **Join maintenance** (`_join_delta`, one rule for every `how`): take the changed join-key values from both sides, `K = πₖ(δL) ∪ πₖ(δR)`, recompute the join restricted to `key ∈ K` over the new states (+1) and the old states (−1), and consolidate. `_restricted_join` pre-filters both inputs with `(keys) IN (SELECT k0 … FROM K)`, which is sound for any `how` and means a small change never scans the other side. The old state is `consolidate(current ⊎ −delta)`, reconstructed only for changed sources (`_reconstruct_old`).
- **Compilation**: every node is a uniquely named `CREATE TEMP VIEW` (`_view`) that the planner inlines. Measured per-node materialisation lost to inlining at every scale (`plans/trickle-dag.md`, "Gate result"), so the only persistence point is an explicit `.merge()`, which also enables reuse across runs.
- **Columns and pipeline**: internal columns are leaf-qualified as `"alias.col"`. `_pipeline_sql` applies the ordered `filter`/`mutate`/`select` operations (call order, as nested subqueries) over the compiled DAG; `_qualify` rewrites `alias.col` references, and mutated or bare names pass through, so the two namespaces never collide.
  - `.mutate(**cols)` adds computed columns (`SELECT * EXCLUDE` replaces a column of the same name). Columns in one call see the input, so chain calls to build on a new column. A mutated column can be a `pk` but not a join key (`on` only resolves against source leaves).
  - `.select` is optional. Without it the output is `*` (`_star_output`: qualified names become bare, equi-join keys are deduplicated with the `_join_key_finder` union-find, and any other bare-name collision raises).
  - `.filter` after `.mutate` can reference the mutated column. The pipeline is row-local and deterministic, so it distributes over the Z-set delta and stays incremental.
  - `.alias(name)` names a source (`_alias_for` = the explicit alias, else positional `s{i}` in left-to-right leaf order; uniqueness is validated; the spine-PK fast-path regex uses the spine's effective alias). `on` resolves per subtree via `_resolve_col`, preferring the leftmost match on the left, and ambiguity raises.
- **Rule of thumb**: a builder method exists only if the engine can maintain it incrementally. Everything else goes through `.sql()`.
- `.sql(query)` is the comprehensive escape hatch (`.alias()` is required to name the table; a join DAG with no `.select` resolves columns via `*`). It collapses the composition into one materialised relation (`_materialised`), runs `query` (a SQL string, or an Ibis expression compiled with `ibis.to_sql(..., dialect="duckdb")`; ibis is imported lazily and isn't a dependency), and returns a comprehensive-mode builder. `.join`/`.select`/`.mutate`/`.filter` raise after it, but `.merge()`/`.append()` still diff or append-filter the result, so the output delta stays incremental (the aggregate itself still rescans).
- `.schema()` returns `{col: duckdb_type}`; `.to_ibis_schema()` maps it to ibis type strings for `ibis.table(...)` (raising on an unmapped type).
- `pk=` on `.merge()` is required and must be genuinely unique (TypeError if omitted, BuildError if empty).
- A source that didn't change contributes nothing: its delta is empty, so it adds no keys to `K` and the restricted join yields nothing.
- **Comprehensive fallback**: a source that reads `is_full` (bootstrap, coverage miss, or a changed overwrite Ripple) or exceeds its threshold `p` makes that subtree, and so the whole output, recompute via `_full_join`, diffed against the materialised prior output (the last-written main, read rather than recomputed) through `pond.merge_table`.
- `BuildError` covers: a join operand carrying filter/mutate/select/agg/sql, an empty merge key, an ambiguous join key, a `*` output with an unresolvable collision on a non-join-key name, a `.mutate` using the reserved `_duckstring_` prefix, and an output (`.select`/`.mutate`/`*`) that omits the PK.
- **Change-fraction threshold** `pond.trickle(ref, p=0.3)` (per source): if the delta touches more than `p` of the source's current rows, the source is read as `is_full`. `p=1.0` disables the check and skips the count.
- **Strategy overrides** on `.merge()`/`.append()` (both default `True`; measure before changing): `ivm=False` ignores deltas and recomputes the whole output via `_full_join`, diffed against the stored main (it also disables the append spine-PK fast path). `key_filter=False` keeps delta composition but skips the `IN (…)` pre-filter, for changes large enough to approach `p` anyway (threaded via `self._key_filter`).

### `.append()` terminal

`trickle_builder._compute` is the shared step behind `.merge()` and `.append()`. `.append(name, *, pk=None, fail_on_conflict=True, log_drops=True)` writes the composed result to an append-only Trickle, for monotonic transforms where output rows are only ever added (for example an append-only fact stream joined to stable or SCD dimensions).
- `trickle_io.append_zset` consolidates the change. A retraction (`d<0`), or a `+1` whose `pk` is already in history with a different image, is a conflict; the same image again is skipped as idempotent.
- With `fail_on_conflict=True` a conflict raises before writing. With `False` the conflicting rows are dropped (history wins) and, if `log_drops`, appended to a `{name}__droplog` companion (user columns + `_duckstring_d` + `_duckstring_f`). The droplog is append-only diagnostics, published alongside the table like `__changelog` (`publish_plan` exports it without a sidecar entry; `schema_contract` and Iceberg treat it like `__changelog`).
- A `pk` duplicate within one run (with distinct images) always raises, since there's no ordering within a run to choose between them. `pk=None` with `False` skips checks entirely (fast, correct only when duplicates and past changes are impossible).
- A comprehensive recompute is tagged `+1` and filtered against history (identical rows skip, changed ones conflict), so coverage misses and replays don't fail spuriously.
- **Spine-PK fast path** (`_spine_pk_passthrough`): when the output PK is a verbatim `s0.<col>` pass-through of the spine's declared PK and `fail_on_conflict=False` and `log_drops=False`, a dimension change can't alter the result (changed facts are dropped silently either way). The builder then skips composing the change and computes new spine rows (pk not in history) joined to current dimensions (`_full_join(spine_rel=…)`, spine = leftmost leaf), turning a large dimension change into an O(spine delta) lookup. Detection is deliberately narrow (only the verbatim `s0.<col>` form); anything else uses the general path, because a false positive would wrongly drop rows.

### Aggregation (`.aggregate(by, **metrics)` or `.group_by(by).aggregate(...)`; see `plans/trickle-agg.md`)

Implemented in `trickle_io.apply_aggregate` with `duckstring.agg` specs:
- count, sum, mean, min, max, var, stddev
- weighted: weight_total, weighted_sum, weighted_average
- two-variable co-moments: covariance, pearson_correlation, ols_slope, ols_intercept
- payload extremes: argmin, argmax
- semigroup reductions: bool_and, bool_or, bit_and, bit_or
- product: retractable via log-sum-exp (count + n_zero + n_neg + Σlog|x| → `(−1)^n_neg·exp(Σlog)`), returns a float and isn't bit-exact for large integer products
- var/stddev/covariance take `how="sample"` (default) or `"pop"`

The result is a grouped merge Trickle keyed by `by` (the output `pk` defaults to `by`), finished only by `.merge()` (`.append` or further joins raise). Raw accumulators live in a registry-only companion `_duckstring_agg_{name}` (`AGG_STATE_PREFIX`, not published): `_a_cnt`; per additive column `_a_sum/_a_cnt/_a_m2` (`M2=Σ(x−x̄)²`, the centred second moment); per extreme `_a_min/_a_max`; per co-moment pair `_c_n/_c_sx/_c_sy/_c_m2x/_c_m2y/_c_cxy`; per weighted unit `_w_num/_w_den`; per argmin/argmax `_g_key/_g_arg`; per semigroup `_s_val`; per product `_p_cnt/_p_nz/_p_nn/_p_sl`. The published main holds only derived user columns.

- **Numerical robustness**: var/stddev/covariance/correlation/ols use the parallel Chan/Pébay merge-in and merge-out of centred moments, avoiding the cancellation-prone `Σx²−(Σx)²/n`. Partition moments are computed about partition means in a two-pass `dacc`; a comprehensive rebuild uses DuckDB's stable `var_pop`/`regr_*`.
- **Incremental path**: distributive sums fold additively and each partition's centred moments merge in or out per affected group, O(δ). min/max/argmin/argmax/bool*/bit* extend from inserts, but a group with any retraction rescans its current membership (`RESCAN_KINDS`; `current` is the builder's `_full_join()`, passed in when `needs_current` and filtered to retracting groups; append-only input never rescans). Groups are f-stamped so a replay skips groups already applied. The output for affected groups is `new(+1) ⊎ old(−1)` → `apply_zset`.
- **Comprehensive path** (bootstrap, coverage miss, over `p`): rebuild accumulators (`_agg_rebuild`) and `merge_table`. A group whose count reaches 0 is dropped and retracted.

### Accumulation (`.along(col)` + `.accumulate(by, **metrics)`)

Order-dependent per-row scans, as opposed to `.aggregate`'s order-independent reductions. Specs from `duckstring.acc`: sum, count, min, max, first, product, prev, lag, convolution, ema, tema, and `scan` (a custom fold `fn(state, row) -> (new_state, output)` with JSON-persisted state). The `acc.` prefix marks a running value, so `acc.sum` is a running sum.

- `.along(col)` declares the order axis, which must be non-decreasing with freshness (a precondition, not a sort).
- `.accumulate(...)` isn't terminal: output has one row per input row, enriched with its running value in `.along` order within its `by` group.
- Finished by `.append()` (append-only; input monotonic in `.along`) or `.merge()` (`apply_accumulate_merge`, retraction-aware with no monotonic requirement). The merge path keeps per-group end state and splits affected groups with `_classify_affected`: a tail-only append resumes from carried state and folds only new rows, O(new); a group with a past change is re-folded over its current membership and merge-diffed against the prior output, O(group).
- An order-dependent custom reduction is `agg.reduce(fn, init)` via `.along().aggregate(by, m=agg.reduce(...)).merge()` (`apply_ordered_reduce`, the same re-fold collapsed to one value per group). It requires `.along` and can't be mixed with order-independent metrics.
- Append mode keeps per-group fold state in a registry-only `_duckstring_acc_{name}` companion (`ACC_STATE_PREFIX`, not published): accumulators plus the last `.along` value, f-stamped for replay. The scan is a Python fold continued from the tail, the same for all metrics (including recursive ema/tema and buffered lag/convolution/scan), O(new rows) per run. When every metric is a scalar-seed scan (sum/count/min/max/first) with a `by`, a SQL window fast path is used instead (`_accumulate_windowed`: one window pass over the batch plus the carried seed per group; `_ACC_WINDOWABLE`).
- Bootstrap or coverage miss re-folds from scratch (idempotent through `append_zset`'s conflict skip). A row below its group's `.along` high-water mark, or a retraction reaching the scan, raises (the monotonic and append-only contracts).

### Chaining, cost, retention

- `.merge()` and `.append()` return a `TrickleBuilder` rooted at the table just written, so `a.join(b).merge("ab", pk=…).join(c).merge("abc", pk=…)` materialises intermediates mid-chain in one Ripple. This gives the same reuse as splitting into a downstream Trickle (a `c`-only change reuses `ab` instead of recomputing `a⋈b`, and it's the only way to join on a key that exists only after an earlier join), without a second Ripple. It's sequential, so it gives up the parallelism of a separate Ripple under a Wave. The returned handle becomes the next spine; its in-run delta is read from the registry via `trickle_io.read_registry_delta` (dispatching on append or merge), since nothing is published mid-run (threaded as `_spine_delta`). A composed builder still can't be used as a dimension; keep it on the spine.
- **Evaluation order** affects cost, not correctness: put volatile or small sources inside and large or stable ones outside, so most runs reconstruct no prior state. Declaration order is used today (spine first), with no automatic tuning. A large reused intermediate is a sign to split a Trickle or chain through a mid-chain `.merge()`.
- **Determinism**: retractions cancel by full-row identity, so projections must be deterministic (no `now()`, `random()` or unstable aggregates).
- **Retention** (`retain_t`/`retain_n`, opt-in) trims old history and changelog rows at write time. It's a lag SLA, not a correctness concern: a consumer that falls behind the window reads the clean main in full. The watermark is `min(_duckstring_f)` over what remains.
- **Ripple sources**: a changed overwrite Ripple forces the consumer's comprehensive path (there's no old state to retract against, so it diffs `O' − main`); an unchanged one is a stable operand. Any table is a valid `.trickle()` source, and promoting a Ripple to a native Trickle upstream makes its changes incremental with no change for consumers.
- **Sidecar**: mode, PK, floor and source `f` travel in the `_trickle.json` sidecar (every published base table has an entry: `{mode,pk,floor,f}` for a Trickle, `{mode:"overwrite",f}` for plain output; the `f` lets a consumer tell whether an overwrite source advanced) and in the registry meta table `_duckstring_trickle`. `dataplane.publish_plan(con, data_dir, f)` validates non-Trickle tables, exempts Trickle ones, and writes the sidecar.
- **Cross-Catchment incremental draw** (`routes/draw.py?after=&base_after=`, `poller._land_transfer`): the consumer sends `after = landed_after(...)` (the highest part `f` it holds, from part file names), and the producer ships only append-only parts with `part_f > after` (append history, `__changelog`, warm `__band`, `__droplog`, zipped as `{table}/{f}.parquet`), which the consumer drops into its parts directory. A merge main's cold base ships whole, but only when its `f_base` has passed the consumer's `base_after`; the consumer then replaces its base directory and reclaims warm and changelog parts `≤ f_base` (storage only, since reconstruction filters them anyway). The sidecar always travels.
- **Iceberg holds overwrite output only** (`iceberg_plane.export`): only plain overwrite output is committed (one f-stamped snapshot per run, pruned to the latest N by `_prune`). Append-only tables (append history, `__changelog`, `__droplog`) and merge main bases aren't committed, because an append-only table's current snapshot would reference every data file ever written, growing metadata O(runs) per run with no read benefit over flat parts (which prune equally well on Parquet stats). They're served from the flat parts (`IcebergDataPlane._raw_read_select` falls back to `ParquetDataPlane._raw_read_select` for tables not in the catalog), so publishing them is O(change) per run. The flat read honours as-of with a row-level `_duckstring_f <= as_of` predicate (f-homogeneous parts are pruned by stats). Export `_duckstring_f` under `SET TimeZone='UTC'`, because pyiceberg rejects non-UTC time zones.
- **Transient views**: a returned relation must not depend on a shared-name temporary view, because a later call re-creates it and the lazy relation re-binds. `read_delta` inlines its window consolidation as a self-contained subquery, and the builder names per-source views with `trickle_io.unique_name(...)`. Keep it that way.
- **Demo**: `duckstring pond demo --trickle` scaffolds `orders` (append) → `catalog` (merge, CDC on price drift) → `priced` (the builder) → `revenue` (comprehensive aggregate). `--ripple` is the overwrite set. `priced`/`revenue` ship Trickle-shaped puddles.

### Maturity

This is the DBSP incremental-join and distributive-aggregation core, implemented over an acyclic query class with SQL recomputation (micro-batches at `F`, compiled to inline SQL, rather than a maintained indexed dataflow). Done: the DAG of binary incremental joins (all six `how`, bushy and snowflake), incremental aggregation for the distributive and algebraic set plus the weighted family and co-moments (retractable and numerically robust), and order-dependent scans. Not done, in priority order: skewness (needs the rescan or log-sum-exp treatment to stay safe), holistic aggregates (median/percentile) and DISTINCT (these need different retraction-aware operators, so they remain a downstream `.sql()` step), persistent indexed traces across runs (the `.merge()` boundary is the only persisted trace), recursion and fixed points, and automatic incrementalisation. Also deferred: `pond.state_dir` for an external stateful IVM engine.

## Version contract (`schema_contract.py`)

Two checks keep a major line compatible, so a Sink pinning `name@major` is safe.

- **`min_version`** (`pond_to_pond.min_version`) is enforced at deploy (`routes/deploy.py`). A Sink that pins below its Source's selected version, or a Source selection that falls below an existing downstream pin, is rejected with 422. A major bump is the way out.
- **The additive schema check** is enforced at publish, since a Pond's output schema is only known at runtime. The schema is captured per `pond_version` on each accepted run (`pond_version_schema`, migration `005`; reported by the Duck on `run_completed`, stored by `Driver._capture_schema`). The Catchment sends the line's high-water schema as the `begin_run` job's `contract` (`Driver._contract_for`); it's `None` for a first run or a rollback (a selection at or below a previously accepted version is governed by `min_version`, and the gate only applies going forward). The Duck checks its output before publishing (`duck/executor._export_data` → `contract_violations`). A violation raises `ContractViolation`, aborts the publish (live tables keep their last good state), and reports `contract_failed`; the Catchment fails the Source at that Run and blocks downstream. It's an ordinary Pond failure with a contract message, reusing `fail_pond`/`derive_blocked` with no new engine state.
- Breaking changes go through a major bump: the old line keeps running and Sinks re-pin when they're ready. Additive output is always accepted: new tables, new columns, and lossless widening of a column type (`schema_contract.is_widening`: DECIMAL precision/scale that can't shrink, the integer, float and timestamp chains; intentionally conservative, so INTEGER→DOUBLE isn't accepted). Dropped columns, removed tables and narrowing type changes are violations. Widening is accepted because SQL result types follow the expression: a data-dependent branch (a COALESCE over a lookup) can legitimately produce DECIMAL(24,2) and later DECIMAL(25,2), and exact string comparison of types once wedged a live Pond permanently (the gate is forward-only and a failed run publishes nothing, so nothing could re-capture).
- For a genuine narrowing, `Driver.reset_contract` → `POST /api/ponds/{name}/reset-contract` (full) → `duckstring control reset-contract` drops the recorded schema so the next accepted run captures it again, and clears the failure.
- Contract failures carry the stable `CONTRACT_PREFIX` (`schema_contract.py`) in their message. `/api/status` derives `failure_kind ∈ contract|error` from it (so it survives restarts without extra state), and the UI's failed StatusCard shows "Failed · Contract violation" with a hint on how to fix it.
- **Open question** (2026-07-12 review): the schema captured per version is a cumulative snapshot, and a Sink's `min_version` pin entitles it to that whole snapshot, so no check over pins can prove a published column unused. Safely dropping a column within a major would need a new declaration of use (for example column-level `[sources]` pins). This is a design decision for the author, not an implementation gap.

## Web UI (`frontend/`) and playground

The Catchment serves a read-mostly Next.js UI (static export mounted at `/`; `npm run dev` proxies `/api` to a Catchment, default `:7474`). It polls `GET /api/status` about every second and `GET /api/runs`, and POSTs triggers and control actions. Topology is read-only: Ponds come from deployed code and are never authored in the UI.

- `/api/status` (`driver.status()`) carries more than the CLI needs: per-Pond `d_ms` and standing `trigger`, per-Ripple state, intra-Pond `ripple_edges`, `runs_completed`, and the fault fields `is_failed`/`is_killed`/`is_blocked`/`failed_f`/`failures`/`immediate_retries`/`source_retries`. The per-Pond `status` string resolves in the order failed → killed → blocked → running → queued → idle. The route also adds the caller's `access_level` (`full` in open mode) so the UI can gate controls (`store.accessLevel` + `atLeast()`): read sees status, history and data; demand adds the Triggers menu; full adds Control, window editing and Failures. The failure reason is visible at read level; only remediation is gated.
- Tracebacks are full-only: `/api/runs` redacts `traceback` on Pond and Ripple runs below full access (`_redact_tracebacks`), since a traceback can leak paths or connection strings. Read and demand still see the error message. This is enforced on the server.
- `/api/runs` (`driver.run_history`) returns Pond Runs newest first, with params `pond`, `lineage` (upstream only), `ripples` (nest Ripple Runs) and `limit` (≤1000). Each run and ripple carries `status`, `retry` (attempt index), `error` and `traceback`.
- The bottom panel is split in half: `RunHistory` (left, clickable rows) and `RunDetail` (right). RunDetail shows the run's freshness and timing, the per-attempt Ripple list (retries marked `↻N`), and below that the failures, one per source (`<ripple> · message` or `Pond · message`) with the full traceback in a `<pre>`. The Sidebar's Control row is Force/Wake/Sleep/Kill, matching the Trigger row; the Failures section sets retry budgets and shows Clear Failure when failed.
- `/api/data` (`routes/data.py`) reads each Pond's exported Parquet through an in-memory DuckDB connection, never the live registry, so queries never contend with a running Duck.
- `frontend/src/lib/`: `api.ts` (typed client), `store.ts` (zustand poll store, growing-window run feed, the colour palette `THEME_*` and helpers `stateColor`/`consumeEdgeColor`/`nodeFill`/`formatAge`), `types.ts`.
- Colours are defined centrally in `store.ts`. A node's fill is a wash of its rim colour. Brand cyan (`THEME_BRAND` `#06c4e6`) marks the running state and is the default accent for UI chrome with no semantic reason for another colour (for example the Selector banner and its primary buttons). Use other colours only where their meaning applies: red for destructive or failed, green for success, amber for pull, green-yellow for push.
- Built with Next 16. Read `frontend/AGENTS.md` (breaking changes; check `node_modules/next/dist/docs/` before editing frontend code).

The playground (`playground/`) is the standalone in-memory simulation, headed for its own repo and `playground.duckstring.com`. It shares no code with the product UI.

## Fault tolerance

Two retry budgets, both defaulting to 0, live on the Pond and can be changed with `control failure-budget` (`pond_retry` table; seeded on deploy from `pond.toml` / `pond_version` defaults, then owned by the operator):
- `immediate_retries`: Ripple Run retries within one Pond Run (per frontier, consumed by the Duck via `worker.immediate_left`).
- `source_retries` (on change): whole Pond Runs the Catchment re-attempts when a Source updates.

Fault state on `PondState`:
- `is_failed`: a Run gave up and hasn't been superseded.
- `failed_f`: the freshest failed Run, used as the recovery watermark (the run gate uses `start_f`; this is for clearing and telemetry).
- `failures`: the count, compared against `source_retries`.
- `is_blocked`: a required Source is failed, killed or blocked. Derived and propagated downstream by `derive_blocked`.
- `is_killed`: operator Kill; terminal.

A failed Pond only re-runs through the on-change path (`sourceF > startF`, while `failures <= source_retries`). A Run completing fresher than `failed_f` ends the episode. A Pond that is blocked but not failed still drains existing Source output but never solicits. A killed Pond doesn't run at all.

Failure sources (each gives a message; Ripple and Duck exceptions also give a traceback, shown in Run Detail):
- **Ripple error**: the Duck spends `immediate_retries` (per frontier), then reports `failed(F = ripple.startF, error, traceback)`. The Catchment fails the Pond at that Run, counts it and blocks downstream (`engine.fail_ripple`).
- **Duck-level error** (for example a ledger write): the Duck reports `pond_failed` against its last `begin_run` and exits.
- **Dead or silent Duck**: `Driver._check_liveness` (in `scheduler_tick`, for `SubprocessLauncher`) fails an in-flight Pond whose process is gone (`proc.poll()`) or whose last contact is more than 60 s old (`engine.fail_pond` at `start_f`).
- **Stuck Run**: the Duck's watchdog reports `pond_failed` if work is outstanding but no Ripple has been running for 30 s.
- **Kill**: `Driver.kill` terminates the Duck and parks the Pond as killed.

Recovery: a failed Pond with on-change budget re-runs on the next Source change (respawning a Duck); redeploying a fixed artifact clears the failure (`Driver.clear_on_redeploy`); `control clear`, `force` and `wake` clear it manually. Run history has one row per attempt (`ripple_run` keyed on `retry`) with `error` and `traceback`. `_check_liveness` skips failed, killed and blocked Ponds, and `clear` rolls `start_f → end_f` so the abandoned Run isn't failed again.

Concurrency: SQLite connections set `PRAGMA busy_timeout`. DuckDB writes and the Parquet export retry transient lock errors via `core.retry_on_lock`; the export uses a read-write connection (never `read_only`) to avoid clashing with pipelined Ripple connections.

Restarts: in-flight Runs complete without the Catchment (events are buffered and replayed idempotently by `F`). When a Duck starts or restarts it reconciles against its ledger and re-runs only incomplete Ripples. `Driver.reload` rebuilds engine state from SQLite (demand and freshness from `pond_state`/`pond_target` including fault fields and `pond_retry`, `gen` from `pond_run` counts, per-Ripple `end_f` from successful `ripple_run` rows), and `resume_incomplete` re-dispatches any `pond_run` left `status='running'`.

## Catchment database

SQLite `duck.db` at the catchment root. The schema is in `catchment/schema/001_init.sql`, applied by `catchment/db.py:migrate()`; new migrations are numbered SQL files (`002_*.sql`, …). File paths stored in the DB are relative to the catchment root. Freshness and targets are stored as UTC ISO-8601 text.

Identity is split across three tables:
- **`pond_name`**: the named entity (`name`, `kind` ∈ inlet/pond/outlet, `git_branch`).
- **`pond_version`**: an immutable deployed snapshot (`pond_name_id`, `version`, `major`, `source_path`, retry config). Topology and run history key off this.
- **`pond`**: the selected version, one per `(pond_name, major)` → `pond_version` (upserted on deploy). This is "the Pond" and the foreign-key target for all live demand, freshness and graph tables.

Other tables:
- Topology (on `pond_version`): `ripple`, `ripple_to_ripple` (intra-Pond edges, all required).
- Contract (on `pond_version`): `pond_version_schema`, one row per output `(table, column, type)`, captured on accepted runs, with a reserved `primary_key` flag for Trickle.
- Live state (on `pond`):
  - `pond_to_pond`: sink `pond_id` → source `pond_name_id` + `source_major`, so a Sink can deploy before its Source.
  - `pond_state`: start_f/end_f/d_ms/has_pull/has_received_pull plus is_failed/is_blocked/failed_f/failures/is_killed/pull_local.
  - `pond_target` (push target set), `pond_retry` (live budgets), `pond_window` (PK `(pond_id, name)`), `pond_trigger` (PK `pond_id`; kind wave/tide, bound_ms).
  - `pond_duck` (PK `pond_id`): a per-Pond compute override (duck_target/dedicated_*/flock_mode/flock_engine/oom_policy; migrations `018` and `021`). The old s/m/l/xl size and flock boolean were removed in `022`; sizing uses concrete instance types and the Flock envelope comes from the real memory cap. It's layered over the compute declared in `pond.toml` and stored on `pond_version` (duck_pool/flock_mode/flock_engine/oom_policy): effective = override, else declared, else default. See `plans/cloud-config.md`.
  - `pond_spout`: egress bindings (see Egress).
- Catchment-level:
  - `duck_pool` (PK `name`): named remote compute pools (instance_type/min/max/idle_timeout/keep_warm/region; migration `021`). CRUD via `Driver.add_pool/list_pools/remove_pool` → `GET/POST/DELETE /api/catchment/duck-pools` (full) → `duckstring duck pool ls|add|rm`. `Driver.duck_pool_names` caches resolution; a `duck_target` naming an undefined pool falls back to the Catchment Duck.
  - `catchment_setting` (key/value): holds the persisted `data_root`, the S3 data-plane target (migration `021`; `catchment/cloud.py`, `GET/PUT /api/catchment/settings`, `duckstring catchment settings [--data-root]`). It can only be set before any Pond has published, since switching afterwards would strand data. `cloud_enabled` (on `/api/status` and settings) requires a remote data root (s3/gs) and AWS credentials (env or an `AWS_*` secret); the UI greys out remote compute until both are present.
- History (on `pond_version` + freshness `f`): `pond_run` (`status` ∈ running/success/failed/killed, `error`, `traceback`) and `ripple_run` (PK includes `retry`, one row per attempt; `status`, `error`, `traceback`). `started_at`/`finished_at` are the Duck's wall-clock execution span from the `ripple` event, used for UI durations. All timestamps are timezone-aware UTC ISO-8601.
- The per-Pond run ledger isn't in `duck.db`. It lives at `ponds/{name}/m{major}/pond.db` (owned by `engine/pond.py`) and is the Duck's operational and recovery record (`ripple_run_state`, `pond_run`). The Catchment's `pond_run`/`ripple_run` are the canonical history.

## Orchestration model

Duckstring schedules by freshness. The Pond is a packaging and versioning boundary; its start and end are zero-duration boundary nodes held as Pond state, so Ponds and Ripples follow the same rules.

- **Freshness `F`** is a UTC timestamp per node: the run-start time of the oldest root feeding it (with windows, the window end the data is fresh until). `NEVER` (`datetime.min`) marks never-run. Staleness = `now + D - F`.
- **Pull** (Tap/Wave) is a `hasPull` token. A node runs when a parent is fresher (`sourceF > startF`) and re-arms its parents when it starts. Cold-start guards use `startF` (`source.startF <= this.startF`).
- **Push** (Pulse/Tide) is a set of unsatisfied target freshnesses. A node runs when `sourceF >= min(targets)`, clearing every target reached. Starting a Pond Run stamps every Ripple with `Pond.startF`. The control verbs map onto these: wake is a non-propagating one-shot pull, force is a recompute at the same freshness, sleep clears demand, kill terminates.
- **No-change skip** (`plans/no-change-skip.md`, migration `013`): a second stamp `PondState.changed_f` (≤ `end_f`) records when the Pond's output last actually changed. `start_f`/`end_f` advance on every run; a pass advances them but holds `changed_f`. In `start_pond_run`, a Pond with Sources completes in the engine as a pass (no `BeginRun`, no Duck; drained by `drain_passes` → `_record_pass`, which writes a no-change `pond_run`) when `max(Source.changed_f) <= prior_f` (strictly against the prior run's freshness, not the new `start_f`). `must_run` (Inlet, `always_run`, force, refresh, repair, draw, spout) bypasses the skip. The Duck reports `changed: bool` on the run-completing `ripple` event and `run_completed` (passed to `complete_ripple(changed=)`). `pond.skip()` (a per-`f` skip signal) and `pond.sources_changed()` (the engine's verdict, passed via `BeginRun.sources_changed` → job → `DuckCore`) support side-effecting Ripples, and `@ripple(always_run=True)` sets `Pond.always_run`. For Trickles the no-change signal is left to the user, as for overwrite: `merge_table`/`append_table`/`apply_zset` return whether anything changed, the builder terminal exposes `.was_changed()`, and the user combines these and calls `pond.skip()`.
- Four subtle rules are encoded in `engine/` and guarded by `tests/test_engine.py`; preserve them: the `startF` cold-start guards, the push target being a set, the Tide clock reference `max(targets) ?? startF`, and the run-start Ripple stamp.

## Testing

`pytest`, with time budgets via `pytest-timeout` (`timeout = 5` by default in `pyproject.toml`; simulation and integration tests override it). Pure engine tests are behavioural simulations driving `sentinel`/`tick` over simulated time in 100 ms steps; never sleep. `tests/conftest.py` sets `DUCKSTRING_SLEEP_MULTIPLIER=0.01` and `DUCKSTRING_DISABLE_DUCKS=1` for the session.

Notable suites: `test_engine` (the validated engine), `test_engine_split`, `test_duck` (buffering, replay, recovery), `test_restart` (restart restore), `test_window`, and `test_runtime` (e2e with real subprocess Ducks on the demo Ponds; enables Ducks and a live server).

Demo Ponds in `src/duckstring/demo/`:
- The Ripple set: `transactions`, `products` → `sales` → `reports` (bottleneck: `sales.join_lines`, 3 s).
- The Trickle set: `orders`, `catalog` → `priced` → `revenue`. `test_trickle_chain_runs_end_to_end` is the deployed-Duck e2e; `tests/test_trickle.py` and the Trickle cases in `test_iceberg.py` cover unit and Iceberg paths.
- Two real-data Trickle sets (`plans/real-data-testing.md`): TPC-DS (`duckstring pond demo --tpcds`, six `tpcds_*` Ponds: a `dsdgen` fact and dimensions → a three-way builder join → two independent Outlets; streaming is emulated by appending a batch per run) and GHArchive (`--gharchive`, six `gh_*` Ponds: the public hourly event stream fetched via httpfs → one Inlet feeding two separate paths). Both are sized by `DUCKSTRING_TPCDS_*` / `DUCKSTRING_GHARCHIVE_*`; the e2es `test_tpcds_demo_chain_runs_end_to_end` and `test_gharchive_demo_chain_runs_end_to_end` shrink them, and GHArchive runs offline against a two-hour fixture in `tests/fixtures/gharchive/`.
- The dbt-mode set (`shop_orders` + `shop_analytics`; `duckstring pond demo --dbt`) with `test_dbt_mode_pond_chain_runs_end_to_end` (skips without the dbt extra).

## Before finishing any code change

Run `ruff check .` and fix all errors (line length 128; rules E/F/I/B).

## Conventions

- Table names are singular (`pond`, not `ponds`).
- Association tables are `{parent}_to_{child}` for many-to-many and `{child}_in_{parent}` for nesting.
- Foreign key columns are `{table}_id`, qualified (`sink_id`, `source_id`) when two reference the same table.
- Keep inter-Pond and intra-Pond concerns in separate tables.
- Freshness and demand state is keyed on `pond` (the selected version); topology and run history on `pond_version`.
- `pond.f` exposes the run's freshness to Ripple code (the Duck passes each Ripple's `start_f` through the executor; the local runner stamps one `now()` per run). It's stable across crash replays and immediate retries, which re-run at the same F, so it works as a watermark or provenance stamp.
- Don't use DuckDB replacement scans (referring to a Python local as a SQL table, `FROM raw`) in Ripple, demo or docs code. They resolve by scanning Python frames and are flaky under the Duck's threaded executor ("don't know what type:" failures on CI). `Pond.read_table` registers foreign Source tables as temp views named after the table, so SQL can use the table name directly; own tables are queried directly; otherwise compose relations with the relation API (`.union`, …).
