# Cross-Catchment Ducts — design & implementation plan

Status: design settled, not yet started. Captures the decisions from the design thread.

## Goal

Let one Catchment consume a Pond that lives in another Catchment, so different teams (or
different compute tiers) can own different parts of the package graph without sharing one
Catchment. A consumed remote Pond appears locally as a **Pond Draw**: a node that draws its
data over the boundary. This also delivers multi-node compute as a side effect (put a
heavy Pond on its own Catchment and duct it in).

On-positioning: this is the package model scaling to org boundaries ("version each transform,
declare its dependencies" across teams/infra) — **not** distributed orchestration. Describe it
as the package graph spanning Catchments.

## Core model

- A **duct** is a one-directional conduit from an upstream (producing) Catchment into a
  downstream (consuming) Catchment. It carries the upstream's address + credentials. Data flows
  down it; demand signals ride back up.
- A **Pond Draw** is the downstream representation of a consumed upstream Pond — a real local node
  (see "real vs synthetic" below) whose single **draw** ripple performs the data transfer.
- A Pond can be marked **open** = "accepts demand from any source". Under single-level auth this
  is a no-op gate (placeholder); its only live effect today is holding the `--tap-on-get` flag.
  Real gating waits for three-level auth (next stage).

## Settled decisions

### Trust / auth
- v1 is **single trust domain**: a duct is a full-trust relationship (the downstream holds the
  upstream's credential, which under single-level auth is full admin of the upstream). Honest
  scope = Catchments one operator administers (e.g. your own compute tiers), **not** untrusted
  cross-team. Document this; don't imply `open` is a boundary yet.
- Soliciting (forwarding demand upstream) is supported from day one — it's the main value prop.
  Under single-level auth a downstream Catchment "just sends demand like any client" via the
  upstream's existing trigger routes. No new demand route, no allow-list (nothing to key on
  without per-caller identity).
- **Next stage**: three-level auth (Read / Run / Write). Build **Read-vs-rest first** — that's the
  split that unlocks untrusted cross-team consumption (scoped per-link token: upstream mints a
  token scoped to "read + solicit pond X", downstream stores it in the existing
  `catchment connect --header` slot). Run-vs-Write is an intra-trusted refinement, lower priority.

### Credentials at rest
- Store in **duck.db**, creds in their own column, **chmod duck.db 0600**, and **redact the auth
  column from `catchment download`'s archive** (the archive already special-cases `-wal/-shm`).
- Rationale: duct metadata is relational state `reload` reads; a sidecar file means a
  join-by-convention across two stores. Reach for a sidecar/env-injected secret only if/when
  encryption-at-rest or platform env-injection becomes a real requirement.

### Data transport
- Incremental is an **optimisation, not a prerequisite**. At <50M-row single-node scope, a full
  parquet copy on F-advance is seconds; `pond.f` is the cache key for free (refetch when remote F
  advances past the landing-zone F). Reserve `--incremental` on `duct add` as a (two-sided,
  producer-negotiated) flag; don't gate the feature on Trickle.
- **Fetch order is a correctness landmine**: copy parquet into the landing zone *first*, then
  advance the Draw's `end_f`. Otherwise downstream runs against absent/stale data.
- Copy-all now; **scope-to-ripples later**. Keep it additive by giving the draw route an optional
  table filter from the start (don't build the filter, just don't design it out).

### The draw route (raw-file fetch)
- `GET /api/ponds/{outlet}/ripples/{ripple_name}` already streams raw exported parquet, but
  **per-ripple only** (one table, no listing). The missing primitive is "stream all of a Pond
  line's exported parquet".
- Add a **separate `GET /api/draw/{name}/{major}`** route (lists + streams all exported parquet;
  optional table filter reserved). Separate route, not overloaded `get`, because the route split
  is what keeps **tap-on-get** off catchment fetches: tap-on-get lives on `/api/query` (and
  optionally per-ripple `get`) only; the draw route never taps. So a Catchment's fetch never
  triggers tap-on-get, for free — no duct-origin tagging needed.

### tap-on-get
- A read-side option on `open` (`catchment open {pond} --tap-on-get`). It **emits the existing Tap**
  on a data read — not a new trigger type (it coexists with Tide etc.). Must **serve the current
  snapshot immediately, never block** on freshness (avoids latency + thundering herd). Lives on the
  query route only, so catchment draws ignore it.

### Pond Draw: real, flagged `is_draw`
- The "transfer is a ripple, with running/idle states and `endF` only on copy-complete" decision
  forces this: running-state + run history must hang on a real `pond_version` + `ripple` (FKs).
- Model as **`kind='inlet'` + `is_draw=1`**: behaves as a local Inlet (freshness root, no local
  upstreams) whose data arrives by transfer. Reuses inlet semantics; renders as a Pond for free.
- **Minimal-real**: a single synthetic `pond_version` + one `"draw"` ripple, written by the duct
  lifecycle (`duct add`/`sync`), **not** the deploy route; cleaned up on `duct remove`/`destroy`.
  `source_path` gets a sentinel (no code).
- The Sink-load worry does **not** bite: `source_majors` comes from the *Sink's own* `pond.toml`
  pin, not from the source being real, so `read_table` resolves correctly regardless.
- **Two essential special-cases**: (1) `pond_source_f` treats a Draw's freshness as externally-set
  (the polled remote F), not the inlet default of `now`; (2) the dispatch path routes a Draw's run
  to the poller's transfer instead of spawning a Duck. Also add `is_draw` to the `_check_liveness`
  skip set (no process to poll).
- Free bonus: `pond_name.name` UNIQUE makes a colliding local deploy fail at the DB — a first
  tripwire toward the deferred namespace guard (needs a friendly error wrapper later).
- Draw keys as plain `name@major`, so an existing local sink's `[sources] = ["sales"]` resolves to
  it with **no `pond.toml` change** — local-vs-ducted is decided operationally.

### CLI surface
```
# Consumer side (the duct lives on the downstream Catchment)
duckstring catchment duct create {upstream} [--sync] [-c {consumer}]   # --sync = create then sync
duckstring catchment duct destroy [-c {consumer}]
duckstring catchment duct sync   [-c {consumer}]                       # bulk-add all current upstream Ponds
duckstring catchment duct ls     [-c {consumer}]
duckstring catchment duct add    {pond_name} [-m {major}] [-c]          # add one Pond  (no --version)
duckstring catchment duct remove {pond_name} [-m {major}] [-c]

# Producer side
duckstring catchment open  {pond_name} [-m {major}] [--tap-on-get]
duckstring catchment close {pond_name} [-m {major}]
```
- `duct create {upstream}` forwards the CLI's stored registration for `{upstream}` (URL +
  `auth_headers(cfg)`) into the consumer Catchment to persist server-side.
- `create`/`destroy` (not `open`/`close`) for the duct, so `open`/`close` stay the pond-level verbs.
- `--catchment/-c` selects which registered Catchment the CLI talks to (the consumer); default
  applies as everywhere else.
- No `--version` on duct ops (demand/freshness is per major).
- No `mode` on the duct: `duct sync` is always its own operation; `--sync` is just shorthand.

### Naming
- "duct" deliberately (short, conduit-meaning, not software-overloaded). Caution: `duct` vs the
  existing `duck` (worker) channel differ by one letter — use a deliberate convention in route/
  module names so they don't get misread. Draw routes live under `/api/draw/...`; the duct's own
  CRUD under `/api/duct/...`.
- "draw" for the fetch route, the synthetic node (Pond Draw), and the transfer operation.

## Explicitly parked (out of MVP)

- **Windows on non-Inlets** — touches freshness landmines (cold-start `startF` guards, run-start
  stamp); do it later, test-first. Intended semantics when done: runs only during a window AND when
  normal run conditions hold; `startF = window.end` when it runs; Inlet = the `sourceF = now`
  special case.
- **Namespace + deploy-collision guard** — block (no auto-redirect) when `name@major` already
  exists as a Draw; best-effort cross-link check; document the concurrent-deploy race + the
  cross-catchment cycle blind spot.
- **Rich status / UI** — for now a Draw renders as an ordinary Pond (running during transfer, idle
  after; demand indicators mirror the upstream Pond). No bespoke rendering yet.
- **Continuous auto-resync** (subscribe mode) — sync is manual for now.
- **Three-level auth** — `open` is the placeholder that earns its keep when this lands.

## Implementation plan (MVP)

### A. Producer side
1. Migration `002_*.sql`: `pond_open (pond_id PK REFERENCES pond, tap_on_get INTEGER DEFAULT 0)`.
2. CLI: `catchment open {pond} [-m] [--tap-on-get]`, `catchment close {pond} [-m]`.
3. Routes: open/close CRUD; `GET /api/draw/{name}/{major}` (lists + streams all exported parquet;
   optional table filter reserved). Add tap-on-get to `/api/query` only (fire Tap, serve current
   snapshot, never block).
4. Demand: reuse the existing trigger routes — no new endpoint.

### B. Consumer side
5. Migration: `duct (id, origin_catchment, remote_url, auth_json, created_at)` +
   `duct_to_pond (duct_id, source_pond_name, major, incremental)`. chmod `duck.db` 0600; redact
   `auth_json` from the archive export. Draws are created as real identity rows
   (`pond_name`/`pond`/`pond_version` + one `"draw"` ripple, `kind='inlet'`, `is_draw=1`) — add
   `is_draw` to the `pond` (or `pond_name`) schema.
6. CLI: `duct create/destroy/sync/ls/add/remove` (see surface above). `duct create` forwards the
   `{upstream}` CLI creds into the consumer.
7. Routes: `/api/duct` CRUD on the consumer (persist duct + members + creds; create/destroy Draw
   identity rows on add/remove/sync).
8. Engine: `Pond.remote`/`is_draw` flag (`engine/core.py`). `reload` (`driver.py`) builds Draws as
   inlet-like source nodes keyed `name@major` and wires them where a local sink references them.
   Never dispatch a `BeginRun` to the launcher for a Draw. `pond_source_f` treats Draw freshness as
   externally-set.
9. Poller (new lifespan task beside `_scheduler` in `app.py`): per duct → GET remote `/api/status`;
   on a member's remote F advancing → mark the draw ripple running (`start_f` = remote F), fetch
   parquet via `/api/draw` into `ponds/{name}/m{major}/data/` **first**, then set `end_f` = remote
   F, idle the ripple, mirror upstream demand + `is_failed/is_killed/is_blocked`, `driver._process`.
   Remote unreachable → Draw `is_blocked`.
10. Soliciting: dispatch path for a Draw carrying pull / a downstream standing wave → forward
    `tap`/`wave` to the upstream's trigger route via the stored duct creds.
11. Confirm the Duck's `source_majors` includes duct-sourced Ponds so `read_table` resolves the
    Draw's major (it derives from the sink's pin, so this should already hold — verify).

### C. Tests + close-out
12. `test_engine`: Draw freshness (endF on transfer-complete), blocked-on-unreachable, soliciting
    forwards demand. New `test_duct` driving two in-process Catchments (or a mocked remote over
    httpx) for poll → fetch → solicit. `ruff check .` before finishing.

### Suggested build order
Migration + the `is_draw` reload/freshness/dispatch path first (the Draw representation is what
everything hangs off), then the draw route + poller + soliciting (the behavioural core), tested
hardest.
