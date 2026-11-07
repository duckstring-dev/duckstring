# Apps: serving an application from the Catchment

Status: **agreed in outline with the author on 2026-11-05; part 1 built 2026-11-06** on the `apps` branch (see
"As built" at the end). v1 runs Apps on the Catchment only.

## Why

A data team's pipeline usually ends in something people use: a dashboard, a small internal tool, an API
over a few tables. Today that is a separate piece of infrastructure, typically a FastAPI or Streamlit app on
its own always-on machine, reading the Catchment's data over the network. Most of those apps are used a few
times a day, so the machine is mostly idle.

The Catchment is already always on, already holds the data, and already builds Python environments from a
locked `pyproject.toml`. Running the App there removes a machine, and the App becomes part of the pipeline it
reads: its Sources are declared, scheduled and versioned like any Pond's.

## The model

An **App** is an Outlet that serves HTTP instead of running Ripples.

- It is deployed like any Pond (`duckstring pond deploy`), with a `pond.toml`, a version, a major line, and
  an optional `pyproject.toml` + `uv.lock` built into a Pond environment (`environments.ensure_env`).
- It declares `[sources]` like any Pond and may read several. Sources give the graph, demand, lineage and
  version pinning; they are not a permission (code on the Catchment can read the whole root, as a Pond on the
  `catchment` Duck can today).
- It is always terminal. It publishes nothing, so deploy refuses any Pond that names an App as a Source.
- Its schedule is a Refresh, set on the App like on any Outlet (`refresh add sales_dashboard --every 1h`):
  demand goes from the App to every Source it reads. That is the pull model's premise applied literally: the
  thing people look at is where demand starts.
- Refreshing on use is opt-in (below). Without it or a Refresh, the App serves whatever its Sources last
  published.

### Declaration

```toml
[pond]
name = "sales_dashboard"
version = "1.0.0"
type = "app"

[sources]
reports = "1.2"
sales = "2.0"

[app]
command = "uvicorn app:app --port $PORT --root-path $DUCKSTRING_APP_PATH"
static = "frontend/out"        # optional: files the Catchment serves itself
idle_timeout = "30m"           # stop after this long without a request; start on the next
state = "local"                # "local" (a durable state directory) or "none"
health = "/healthz"            # optional: ready when this answers 2xx; else when the port accepts
```

Stored as kind `outlet` (the `pond_name.kind` check allows only inlet/pond/outlet) with `pond.is_app`, as a
Spout is an Outlet with `is_spout`. `type = "app"` and Ripples in the same Pond are a deploy error.

### What can run

The contract is a process, not a framework: a `command` that serves HTTP on `$PORT` under the base path
`$DUCKSTRING_APP_PATH`, run in the App's environment. FastAPI, Flask, Django, Streamlit, Dash, Panel, Gradio
and Shiny for Python all fit; the docs carry a recipe per framework, since each sets its base path
differently. `static` covers anything that builds to files (a Next.js static export, Evidence, Observable
Framework).

v1 is Python-only for the process, because Pond environments, the Duck image and the environment code
already exist. Node servers and other languages come later through a container image, which fits remote
compute better anyway.

## Address

A subpath on the Catchment's own port:

- `/apps/{name}/` follows the served major (as the catalog's bare `{pond}` schema does with `pond promote`).
- `/apps/{name}/v{major}/` is a specific major.

No DNS is needed. The routes mount before the web UI's `/` catch-all. The forwarding handles WebSockets
(Streamlit, Shiny and Dash need them; `uvicorn[standard]` already brings the `websockets` package) and streams
responses. When the Catchment itself runs under a subpath (Posit Connect's `/content/{guid}/`), the App's
base path must carry that prefix too: the Catchment learns it from the request (as it learns its bound
address), so `DUCKSTRING_APP_PATH` is the full external path.

The forwarding drops the Catchment's own credentials (an `Authorization` header carrying a Catchment key)
before a request reaches the App, so an App that logs requests never logs a key.

## Access

Requests reach an App through the same gate as the Catchment's web UI: platform auth when hosted, the
built-in keys when self-hosted, nothing on an open Catchment. Beyond that, sign-in is the App's own business
(any Python auth library, or a private network). Batteries-included end-user sign-in (SSO, per-App viewer
lists) is out of scope for the open source version.

The App's own data access needs no key: it reads through its Pond handle (below), as a Pond does.

## Lifecycle: instances

An App has no Runs. Its unit is an **instance**: one process, from start to stop.

- **Start**: on the first request after deploy or after an idle stop. The request is held (up to a limit,
  then a 503 with a "starting" page) until the instance is ready: `health` answers 2xx, or the port accepts
  connections. Starting a process on the Catchment takes a second or two.
- **Stop**: after `idle_timeout` with no request, on redeploy (the new version's instance replaces it), on
  Sleep or Kill, or on removal. `idle_timeout = "0"` keeps it running.
- **Failure**: an instance that exits unexpectedly or never becomes ready fails the App like a Pond:
  `is_failed`, alerts fire, and `immediate_retries` decides whether it restarts at once. Control verbs apply:
  Wake/Clear clear the failure, Kill stops the instance and parks the App.
- **Logs**: an instance's stdout and stderr go to `apps/{name}/m{major}/logs/{instance}.log`, read with
  `duckstring app logs sales_dashboard [--instance ID] [--follow]` and in the Sidebar. This is the one new
  surface: a Ripple leaves a traceback, an App only has its output.

`app_instance (id, pond_id, pond_version_id, started_at, stopped_at, stop_reason, pid, port)` records
instances. With scaling (later) several instances of one App run in parallel, and each keeps its own logs.

### In the engine

The App is an engine node so that demand and freshness work unchanged, but it has nothing to compute. Its
freshness is the freshness a Pond Run would adopt if it started now: `pond_source_f` over its Sources, with
the same rules for optional Sources and windows as any Pond. Demand on the App is met as soon as that
freshness reaches it. A `BeginRun` never goes to a Duck: like a Spout's it goes to a worker, which completes it
at once. Those completions are not recorded as `pond_run` rows (the Sidebar shows instances instead), and an
App gets a synthetic Ripple as a Spout gets `egress`.

### Memory

A running instance takes memory slots (`plans/memory-budget.md`) like a Pond Run, and holds them until it
stops, so an idle stop returns them. The App's `[pond] memory` is its reservation, and the docs recommend
setting one. An instance waits for its slots like a Pond Run does (FIFO), holding the request that started
it, up to the same limit as a start. The grant becomes the handle connection's DuckDB `memory_limit`; the
process as a whole isn't capped in v1 (that would need cgroups), so the reservation is advisory beyond
DuckDB.

## State

`state = "local"` gives the App a durable directory, `apps/{name}/m{major}/state/`, passed as
`$DUCKSTRING_APP_STATE`. It lives outside the version directory, so it survives redeploys, as a Pond's
registry does. What the App keeps there, and in what format, is its own business.

`catchment download` and the state backup never copy an App's files as they are, since only the App knows
which of them matter, how to copy them while it runs, and which are secret. An App opts in with
`[app] download`, a command that writes into `$DUCKSTRING_APP_DOWNLOAD` what its state directory should
hold after a restore. Without it, the App's state isn't included (decided 2026-11-06, over copying the whole
directory or leaving downloads to an API the App serves itself). See "As built (part 3)".

An App can write anything it likes: SQLite in its state directory, or an external database such as Postgres.
It cannot publish Duckstring tables or Objects. To bring an App's data into a pipeline, the App serves it
over an API and an Inlet reads that API like any other external source. The engine never sees that loop, so
it can't deadlock (the Inlet is a root driven by its own Refresh); lineage doesn't show it either, as with any
external source.

`state = "none"` declares the App keeps no local state. Only such an App can later scale out to remote
replicas: shared state across machines is left to an external database. (`fractfs` was considered for
checkpointing App state and rejected: it is single-replica by design and copies a live SQLite file without
the backup API.)

## The Pond handle in an App

An App gets a Pond handle like any Pond, so code reads Sources the same way a Ripple does:

```python
from fastapi import FastAPI
from duckstring import app

pond = app.pond()          # the App's handle: its Sources, pinned to their declared majors
api = FastAPI()

@api.get("/top")
def top(limit: int = 10):
    rel = pond.read_table("reports.monthly_summary")
    return rel.order("total_revenue DESC").limit(limit).fetchall()
```

What differs from a Ripple's handle:

- **Read-only.** `read_table`, `count_table`, `read_object`, `object_path`, `source_columns` and `con` for
  SQL. No own tables, no writes, no `read_delta` or `trickle` (an App has no `previous_f` window).
- **Reads the latest.** A Ripple's reads are pinned to its Run (`source_f`, `as_of=f`); an App's resolve to
  each Source's newest publish at the time of the call. Data can change within a user's session, which Apps
  generally tolerate within a major version. `pond.freshness()` gives the App's freshness and each Source's,
  so the App can show "data as of …".
- **Threads.** Web servers serve requests on several threads, and a DuckDB connection isn't safe to share
  across them, so the handle gives each thread its own cursor.

### Local development

`duckstring pond run` on an App serves it locally with the handle rooted at `puddles/`, so
`read_table("reports.monthly_summary")` reads the Puddle, and `pond hydrate` (including `--from-catchment`)
fills the Puddles as it does for a Pond. The command runs in the App's own `.venv`, as `pond run` does today.

## Refresh on use

Opt-in, and still called a Tap: `refresh tap add sales_dashboard --fresh-within 1h` on an App means "a
request to this App refreshes its Sources when their data is older than an hour". It reuses `engine.tap_due`
and `refresh_now` on the App node, counted per request by the forwarding instead of per query. An App's Tap
mints no key, since its requests already come through the Catchment's gate.

## Keeping versions an App is reading

A Source keeps an old version while a query is reading it and drops it once the query completes. Today
`Driver._retain_from` keeps the version each in-flight Sink Run pinned plus everything newer, and the line's
current version; a publish prunes the rest (`prune_versions`, `gc_tiers`). An App read takes the newest
version at the time of the call, so it survives the next publish but not the one after. Long reads under a
frequent Refresh can lose their files.

Proposal: the Catchment's forwarding holds a **lease** per request in flight, on each Source line's
`changed_f` at the moment the request arrived, and `_retain_from` lowers its bound to the oldest lease, as it
does for a Sink's pin. A read inside the request can only resolve to that version or a newer one, so it is
always kept. A WebSocket connection (a Streamlit or Dash session) renews its lease at each message instead of
holding the version from its start, so a long session keeps one recent version, not every version since it
opened. Work an App does outside a request (a background thread, a startup cache) takes a lease
explicitly with `with pond.hold(): ...`. Leases are in memory and end with the request; a Catchment restart
drops them along with the instances.

## v1 scope

In:

- `type = "app"` Ponds on Catchment compute only.
- A Python `command` in the App's environment, plus `static` files.
- `/apps/{name}/` forwarding with WebSockets and streaming.
- Instances: start on request, idle stop, health, failure, logs.
- The state directory and its place in `catchment download`.
- The read-only Pond handle, and `pond run` against Puddles.
- Read leases in `_retain_from`, and memory slots for instances.
- Refresh and optional refresh on use.
- The web UI: an App node on the canvas, its instances and logs in the Sidebar, a link to open it.

Deferred:

- Remote compute (a dedicated Duck or Fargate) and scaling out to several instances. Remote Ducks only dial
  out today, so forwarding to one needs an inbound rule or a reverse tunnel. These are for scaling: the
  baseline instance always runs on the Catchment.
- Container images and non-Python runtimes.
- End-user sign-in (commercial).

## Settled (2026-11-05)

- **Retention**: an active query keeps the version it reads; it is dropped once the query completes (the
  lease above).
- **Freshness**: the freshness a Pond Run would adopt now, with the usual rules for optional Sources.
- **Memory**: an instance takes slots; setting a reservation is the recommended practice.
- **Naming**: refresh on use for an App is a Tap.

## Open questions

- **Where the lease ends for a streamed response.** A response streamed after the handler returns (a large
  CSV download) still reads; the lease should last until the response body finishes, which the forwarding
  sees, not until the handler returns.
- **Whether the sandboxed serving connection has the same gap.** It materialises tables per connection, so
  probably not, but the full-access serving connection's views read files in place and may.

## As built (part 1, 2026-11-06)

Built: the declaration and deploy, the engine node, instances (start on request, idle stop, health, failure,
logs), forwarding with WebSockets and the sign-in gate, static files, the read-only handle, `pond run` against
Puddles, `duckstring app instances|logs`, the instance and log routes, reference docs. Tests: `tests/test_app.py`.

Where it departs from the plan above:

- **The base path is stripped by default.** `uvicorn --root-path` prepends the root path to the request path, so an
  unstripped request reached FastAPI as `/apps/x/v1/apps/x/v1/...`. ASGI and WSGI servers expect the proxy to strip
  it; Streamlit's `baseUrlPath` and Dash's path prefixes expect it kept. `[app] strip_path` (default `true`) chooses,
  and `X-Forwarded-Prefix` names the base path either way.
- **The bare path redirects** (307) to `/apps/{name}/v{major}/`, so every instance has one base path and the App
  doesn't restart when `pond promote` moves the served major. A WebSocket can't follow a redirect, so a bare
  WebSocket path is refused.
- **`command` is optional when `static` is given**, for a site that builds to files (a static-only App never starts
  a process).
- **Freshness** is kept by a standing Wake on every App (as a Spout's), so `end_f` follows the Sources without
  soliciting them. The handle reads it from a `freshness.json` the supervisor rewrites when it changes, since an
  App has no key to call the Catchment with.
- **The state directory** is `apps/{name}/m{major}/state/`, outside the line's runtime directory, so a Catchment
  reset (which scrubs `ponds/*/m*/`) keeps it; removing the line deletes it. Logs live in the runtime directory,
  `ponds/{name}/m{major}/app/logs/`.
- **Browsers on a keyed Catchment** get a minimal sign-in form that stores the key in an `HttpOnly` cookie scoped to
  `/apps/`; the cookie and a bearer key are removed before forwarding.
- **Sleep** doesn't stop an instance (only Kill and removal do); the idle timeout ends it.
- `catchment download` included the state directory as plain files (replaced in part 3).

## As built (part 2, 2026-11-06)

Built: read leases, memory slots for instances, and Taps on Apps. Tests in `tests/test_app.py` and
`tests/test_engine_slots.py`.

- **Read leases** are the proposal above. A request's lease is each Source line's `changed_f` when it arrived and
  lasts until its response body has been sent, which settles the open question on streamed responses. A WebSocket
  renews at each message from the browser and keeps the lease before it for 60 seconds, since the work an earlier
  message started may still be reading. `pond.hold()` writes a file under the App's runtime directory, so the
  Catchment needs no channel back from the App; the handle rewrites it until the Sources' run stamps are stable,
  and a hold whose process has gone is ignored.
- **Memory slots**: `[pond] memory` means what it means for a Ripple, memory held outside DuckDB, so an App takes
  one slot plus enough for it, and DuckDB's `memory_limit` in the handle is the grant less that. The limit is per
  process, so a server with several workers should declare their total. A start that can't get its slots within
  the start timeout answers 503 and leaves the queue. An App never takes the spare slots a lone Run gets, since it
  holds them for as long as it serves.
- **Taps on Apps** have no key (`pond_tap.key_hash` became nullable, migration `035`). Every forwarded request
  runs them, as does every message from the browser on a WebSocket, at most once a second. Requests for static
  files don't.

Still to build: the web UI (the App node, instances and logs in the Sidebar, an Open link), the guide and
framework recipes. The full-access serving connection's views (the other open
question) were not looked at.

## As built (part 3, 2026-11-06): downloads

- `[app] download` is a command, run in the App's directory and environment with `$DUCKSTRING_APP_STATE` and
  `$DUCKSTRING_APP_DOWNLOAD` set (`app_supervisor.run_download`). It runs whether or not an instance is running,
  so a download never starts an idle App. Declaring it with `state = "none"` is an error.
- `catchment download` runs every deployed App's command before the archive streams (`snapshot_apps`), so a
  failure or a timeout (10 minutes) is an error response naming the App, with the tail of its output, rather than
  a broken tar. The output lands at `apps/{name}/m{major}/state/` in the archive, so the extracted root is a
  restore. `usage` estimates it by the state directory's current size.
- Nothing else of an App is copied: not `apps/` and not the runtime directory `ponds/{name}/m{major}/app/`
  (logs, holds, the freshness file). Before this the logs were included too.
- The state backup's shutdown bundle (`state_sync.checkpoint_full`) follows the same rule. There a failing
  command leaves that App's state out with a warning, since a backup is best effort and shutdown can't wait on
  it.

## As built: redeploying an App's running version

- A Pond's running version can't be redeployed without `--force` (a version is an immutable artifact, and a
  warm Duck would keep the old code). An App is exempt, since nothing can name it as a Source: its version
  matters only for the major in its address. The author's call, 2026-11-06.
- The deploy stops the running instance before replacing its directory (`stop_now(..., process=True)`), so it
  never runs from a half-replaced folder; the next request starts the new code. Instances compare
  `pond_version.deployed_at` as well as the version id (`_current`), so one started between the stop and the
  record landing is replaced by the next `reconcile`.

## As built: the web UI (2026-11-07)

- An App node is subtly distinct: a rounder box, no Ripple (the synthetic `serve` is hidden), and an Open button where a Pond shows its table button. Its second line is its state (idle, starting, running, waiting for memory, failed, killed) and its data age; run counts are left out, since an App's Runs are passes. While an instance serves, the rim is the running colour.
- Opening an App from the UI signs the new tab in with the key the UI holds (a form POST to `/apps/_signin`), so the App doesn't ask for it again.
- The sidebar shows the version and when it was deployed (a version can be redeployed in place), Open App, the Sources with their ages, the instance (running since, requests in flight or when it stops for idleness), Refresh with Taps, and Restart and Kill. Options keeps the immediate retry budget and Alerts.
- The bottom panel shows the App's instances and the selected one's log, following a running instance. Logs need full access, as in the CLI.
- **Restart** (`POST /api/ponds/{name}/app/restart`, `duckstring app restart`): clears a failure or a Kill, stops the instance (reason `restart`) and starts a fresh one in the background. It replaces Force, Wake and Sleep in the App's Control row.
- Bulk Rebuild and Repair pass over Apps (nothing to rebuild).
