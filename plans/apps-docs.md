# Apps docs and README: handoff

Facts for writing the Apps guide and updating the README for 0.8.0, gathered 2026-11-08 from the code, the tests
and a live run against a local Catchment. This is a list of facts, not prose: write the pages fresh, following
`docs/documentation_style.md`. `plans/apps.md` has the design and its "As built" sections; this file is what a
reader needs.

Each fact is marked **[live]** (seen working through a real Catchment, browser included where it says so),
**[code]** (from the code and unit tests only) or **[unverified]**.

## Pages to change

- New guide for Apps. "Getting Data Out" (`docs/sidebars.ts`) is the natural home, beside `querying`,
  `serving_apps_with_taps` and `delivering_with_spouts`.
- **Naming overlap.** `guides/serving_apps_with_taps.md` ("Serving Apps with Taps") is about an app hosted
  elsewhere querying through a Tap's key. An App (capital A) now means a Pond that serves HTTP from the Catchment,
  and a Tap on an App is a different thing again (no key; requests to the App run it). Readers will mix these up.
  Options: retitle the Taps guide (for example "Refreshing on Use with Taps"), keeping its slug, since the README
  and other pages link to it; and have each guide say in a line when to use the other. The author's call.
- The Apps guide should link to the existing reference, not repeat it: `reference/pond_toml.md#app` (every key, the
  environment variables, memory, downloads, leases, failure), `reference/cli/app.md`, `reference/http_api.md`
  ("Apps" routes).
- `reference/environment.md`: `DUCKSTRING_APP_START_TIMEOUT` (seconds, default 60) isn't listed. It bounds both
  waiting for memory slots and waiting for the App to be ready.
- `concepts/ponds.md` lists Inlet/Pond/Outlet roles and doesn't mention Apps. Probably one or two sentences there,
  linking the guide.
- README (see the last section).
- Landing page: Apps aren't on it. It is the positioning source of truth, so adding them is the author's call.
- The `[app]` reference example and the error message for a missing `command` were changed in this session to quote
  `"$DUCKSTRING_APP_PATH"` (see "Pitfalls").

## What an App is

- A Pond with `[pond] type = "app"` and an `[app]` section. It serves HTTP instead of running Ripples. Stored as an
  Outlet. **[code]**
- It declares `[sources]` like any Pond and may read several. Sources give it a place in the graph, demand,
  lineage and version pins. They aren't a permission. **[code]**
- Always terminal: deploy refuses any Pond that names an App as a Source, in either deploy order. **[code]**
- It can't have `src/pond.py` Ripples, `[ripples]`, `[static]` or `dbt_project`. **[code]**
- No Runs in the usual sense. Its freshness is what a Pond Run would adopt now, and each Refresh completes at once
  as a pass, recorded in its run history so the UI can chart its data age. **[code]**
- Scheduling is a normal Outlet's: `duckstring refresh add sales_dashboard --every 1h` sends demand to every Source
  it reads. Without a Refresh or a Tap it serves whatever its Sources last published. **[code]**
- Python only in 0.8.0: the `command` runs in the App's Python environment. `static` covers anything that builds to
  files. **[code]**
- Runs on the Catchment's own machine only. Remote compute and several instances are not built. **[code]**

## Declaration and environment

- Keys: `command`, `static`, `strip_path` (default true), `health`, `idle_timeout` (default `30m`, `"0"` keeps it
  running), `state` (`local` default, or `none`), `download`. Unknown keys are refused. An App needs `command`,
  `static` or both. `[pond] memory` is what the process holds outside DuckDB. **[code]**
- A `pyproject.toml` + `uv.lock` gives it its own environment, built at deploy, as for any Pond. The Catchment's own
  Duckstring is installed into it. **[live]**: Streamlit and Dash Apps deployed this way.
- The command runs with `/bin/sh -c "exec <command>"` in the deployed directory, with the environment's `bin` first
  on `PATH` (so `streamlit`, `gunicorn`, `uvicorn` from the lock are found), in its own process group. It must
  listen on `127.0.0.1:$PORT`. **[live]**
- Environment: `PORT`, `DUCKSTRING_APP_PATH` (`/apps/{name}/v{major}`, no trailing slash), `DUCKSTRING_APP_STATE`
  (with `state = "local"`), `DUCKSTRING_APP` (JSON for the handle), `PYTHONUNBUFFERED=1`. `DUCKSTRING_API_KEY` is
  withheld. Other Catchment env vars pass through. **[code]**
- Output (stdout and stderr) goes to a log per instance: `duckstring app logs NAME [--follow]`, or the web UI's
  bottom panel. Logs need full access. **[code]**

## Address and access

- Served at `/apps/{name}/v{major}/` on the Catchment's own port. `/apps/{name}/` redirects (307) to the major
  `pond promote` last chose (the first deployed until then); `/apps/{name}/v1` redirects to `/apps/{name}/v1/`.
  **[live]**
- `strip_path = true` (default): the App sees paths with the base removed, and the base in `X-Forwarded-Prefix`
  (what `uvicorn --root-path` and WSGI servers expect). `false`: the full path, for frameworks configured with
  the base themselves (Streamlit's `--server.baseUrlPath`). **[live]**
- WebSockets and streamed responses are forwarded. **[live]**: Streamlit's `/_stcore/stream` WebSocket, through the
  Catchment, in a browser.
- `static` files are served by the Catchment itself and never reach the process. A static-only App has no
  process. **[code]**
- Access: an open Catchment (or one gated by a hosting platform) lets every request through. With keys, any key
  (read level is enough) as a bearer token, or a browser signs in once on a minimal sign-in page that sets an
  http-only cookie scoped to `/apps/`. The key is stripped before the request reaches the App. **[code]**
- The web UI's Open button signs the new tab in with the key the UI already holds. **[code]**
- End-user sign-in beyond that (SSO, per-App viewer lists) is the App's own business. **[code]**
- Under a Catchment hosted at a subpath (Posit Connect's `/content/{guid}/`), the sign-in form's absolute
  `/apps/` action and cookie path don't fit. **[unverified]**: untested there. Platform auth usually leaves the
  Catchment open, so the sign-in page isn't shown.

## Lifecycle

- An instance is one process, from start to stop. The first request starts one and waits for it: ready when
  `health` (a path below the base) answers 2xx, else when the port accepts connections. **[live]**
- Measured: a first request to a Streamlit or Dash App returned in about 2 seconds, including the start. **[live]**
- Start timeout 60 s (`DUCKSTRING_APP_START_TIMEOUT`); beyond it, a 503. **[code]**
- Stops after `idle_timeout` with no request in flight (a streamed response or an open WebSocket counts as in
  flight), on redeploy, Kill, removal and Catchment shutdown. **[code]**
- Failure: an instance that exits on its own, or isn't ready in time, counts against `immediate_retries` in a row.
  Past them the App fails (an alert fires like any Pond failure) and answers 503 until `control wake`, `app restart`
  or a redeploy. **[live]**: killing a running instance's process left the App failed and answering 503; a
  redeploy brought it back.
- `duckstring app restart NAME`: stops the instance and starts a fresh one, clearing a failure or Kill first.
  `app instances NAME` lists instances with their stop reasons (`idle`, `redeploy`, `restart`, `killed`,
  `removed`, `shutdown`, `exited`, `start_failed`). **[code]**
- Redeploy: an App may redeploy its running version without a version bump (nothing pins it). The running process
  is stopped before its directory is replaced, and the next request starts the new code. A new major runs beside
  the old one at its own address. **[live]** for an in-place redeploy.

## Reading data: the handle

```python
from duckstring import app

pond = app.pond()
rel = pond.read_table("reports.monthly_summary")
```

- `app.pond()`: one handle per process, each thread gets its own DuckDB cursor (web servers use several
  threads). **[code]**
- Read-only: `read_table`, `count_table`, `read_object`, `object_path`, `source_columns`, `con`. Own refs raise
  (always name the Source: `reports.monthly_summary`). Writes, `read_delta`, `trickle` and `skip` raise. **[code]**
- Reads the newest published data at the time of the call (no pin to a Run). **[live]**
- `read_table` registers the Source table as a temp view named after the table on that thread's cursor, so SQL on
  `pond.con` can then use the bare name. **[code]** (as for Ripples; per thread, since each thread has its own
  cursor).
- `pond.freshness()`: `{"freshness": datetime, "sources": {name: datetime}}`, for "data as of ...". Empty outside a
  Catchment. **[code]**
- Read leases: a Source keeps the version a request may read until its response has been sent. A WebSocket holds
  what was published at its latest browser message (and the previous one for 60 s). Work outside a request (a
  background thread, a startup cache) wraps its reads in `with pond.hold():`. **[code]**
- An App can't publish tables or Objects. To bring an App's data into a pipeline, the App serves it over an API and
  an Inlet reads that API. **[code]** (design rule in `plans/apps.md`).

## Memory

- On a Catchment with memory slots (the default, `DUCKSTRING_RUN_SLOTS=4`), a running instance holds slots like a
  Pond Run until it stops: one for its DuckDB plus enough for `[pond] memory`. A start waits for them, first come
  first served, up to the start timeout, then 503 "waiting for memory". An idle stop returns them. **[code]**
- The handle sets DuckDB's `memory_limit` to the grant less `[pond] memory`. It's per process, so a server with
  several worker processes (gunicorn `--workers 2`) should declare their total. **[code]**
- Setting `[pond] memory` is recommended. Nothing caps the process beyond DuckDB. **[code]**

## State

- `state = "local"`: a directory at `$DUCKSTRING_APP_STATE` that survives restarts and redeploys (it's outside the
  version's directory and a Catchment reset keeps it; removing the App's line deletes it). **[code]**
- `state = "none"`: no directory. Use it when the App keeps nothing; it's also the only kind that could later
  scale out. **[code]**
- Downloads and the state backup copy an App's state only through `[app] download`: a command that writes into
  `$DUCKSTRING_APP_DOWNLOAD` what the state directory should hold after a restore (for SQLite, a backup-API copy).
  Failure or 10 minutes: the download fails naming the App; the state backup goes on without it. Refused with
  `state = "none"`. **[code]**

## Refresh on use

- A Tap on an App: `duckstring refresh tap add sales_dashboard --name use --fresh-within 1h`. No key is minted
  (`rotate` is refused): requests to the App already come through the Catchment. Each forwarded request runs it,
  as does each browser WebSocket message (at most once a second). Static files don't. **[code]**
- Same policy as any Tap: a Refresh when the newest request or freshness is at least `--fresh-within` old; the
  request is answered at once from what is published. **[code]**

## Local development

- `duckstring pond hydrate [--from-catchment]` fills `puddles/` for the App's Sources, as for any Pond. **[live]**
- `duckstring pond run [--port 8000]` in the App's directory serves it at `http://127.0.0.1:{port}/` against its
  Puddles, base path empty, state in `puddles/app/state/`, re-run under the App's `.venv` (run `uv sync` first).
  A static-only App is served with Python's file server. **[live]** with Streamlit.
- Fixed in this session: `pond run` didn't put the `.venv`'s `bin` on `PATH`, so `streamlit` wasn't found. Now it
  does, as the Catchment does.

## Pitfalls (found live)

- **Quote the base path.** Locally `$DUCKSTRING_APP_PATH` is empty, and an unquoted empty variable vanishes from
  the command line: `--root-path $DUCKSTRING_APP_PATH` at the end fails in uvicorn ("Option '--root-path' requires
  an argument"), and Streamlit's `--server.baseUrlPath $DUCKSTRING_APP_PATH --server.headless true` swallowed the
  next flag and stopped at Streamlit's email prompt. Write `"$DUCKSTRING_APP_PATH"`, and to keep the double quotes
  inside TOML use a literal string: `command = 'uvicorn main:app --port $PORT --root-path "$DUCKSTRING_APP_PATH"'`.
  **[live]**
- Bind to `127.0.0.1` explicitly where a framework defaults elsewhere (Streamlit's `--server.address`). **[live]**
- `--server.headless true` for Streamlit, or it asks for an email on first run. **[live]**

## Framework recipes

Each was run through a local Catchment unless marked. The docs' own examples should be re-run as written.

FastAPI, `strip_path` default. **[code]** (the App tests run real uvicorn processes through the forwarding):

```toml
[app]
command = 'uvicorn main:app --port $PORT --root-path "$DUCKSTRING_APP_PATH"'
health = "/healthz"
```

Streamlit, in a browser **[live]**:

```toml
[pond]
name = "st_dash"
version = "1.0.0"
type = "app"
memory = "500MB"

[sources]
reports = "1.0.0"

[app]
command = 'streamlit run app.py --server.port $PORT --server.address 127.0.0.1 --server.baseUrlPath "$DUCKSTRING_APP_PATH" --server.headless true'
strip_path = false
health = "/_stcore/health"
state = "none"
```

```python
import streamlit as st
from duckstring import app

pond = app.pond()
st.title("Monthly sales")
st.dataframe(pond.read_table("reports.monthly_summary").df())
```

`pyproject.toml` dependencies: `streamlit`, `duckstring`. The table rendered from `reports`, and the WebSocket
connected at `/apps/st_dash/v1/_stcore/stream`. Also served with `pond run`. Streamlit shows its own "Deploy"
button in the corner; hiding it wasn't tried.

Dash, `strip_path` default, gunicorn with two workers, in a browser **[live]**:

```toml
[app]
command = "gunicorn app:server --bind 127.0.0.1:$PORT --workers 2"
state = "none"
```

```python
import os

from dash import Dash, Input, Output, dash_table, dcc, html
from duckstring import app

pond = app.pond()
dash_app = Dash(__name__, requests_pathname_prefix=os.environ["DUCKSTRING_APP_PATH"] + "/",
                routes_pathname_prefix="/")
server = dash_app.server


def rows(category):
    rel = pond.read_table("reports.monthly_summary")
    if category:
        rel = rel.filter(f"category = '{category}'")
    return rel.df().to_dict("records")


dash_app.layout = html.Div([
    html.H1("Monthly sales"),
    dcc.Dropdown(["Books", "Clothing", "Food", "Home"], id="category"),
    dash_table.DataTable(id="table"),
])


@dash_app.callback(Output("table", "data"), Input("category", "value"))
def update(category):
    return rows(category)
```

Dependencies: `dash`, `gunicorn`, `pandas`, `duckstring`. The page and its callback POSTs went through the
forwarding. (The f-string filter is fine for a fixed dropdown list; a docs example taking free text should bind a
parameter instead.)

Not tried **[unverified]**: Flask (needs Werkzeug's `ProxyFix(x_prefix=1)` to honour `X-Forwarded-Prefix`, or
`strip_path = false` with the routes under the base), Gradio (`root_path`), Panel, Shiny for Python, a static
export (Next.js `basePath`, Evidence, Observable Framework). Only include those the docs session runs.

## Web UI

- On the canvas an App is a rounder box with no Ripples, an Open button, and its state (idle, starting, running,
  waiting for memory, failed, killed) with its data age. The rim is the running colour while an instance serves.
  **[code]**
- Sidebar: version and deploy age, Open App, Sources and their ages, the instance, Refresh (with Taps), Restart and
  Kill. Bottom panel: instances and the selected one's log, followed while running. **[code]**
- Screenshots per `docs/documentation_style.md` media rules: the demo with an App over `reports` would show the
  node, sidebar and the App itself. There's no `pond demo` for an App; the guide can build one from the demo's
  `reports` (as above). Adding `duckstring pond demo --app` is an option for the author.

## Not in 0.8.0 (say so briefly, if at all)

- Remote compute and scaling out to several instances (only `state = "none"` Apps will be able to).
- Processes other than Python (a container image, later).
- End-user sign-in.

## README

Current lines that are out of date for 0.8.0 (README.md):

- "Also included": `[Cloud compute](…/cloud_compute_on_aws) on AWS Fargate or EC2, chosen per Pond, with data on
  S3`. Now AWS (Fargate, EC2), Azure (Container Apps, VMs) and Google Cloud (Cloud Run, Compute Engine), each with
  data on its own object store (S3, ADLS/Blob, GCS); three guides. **[live]** on each cloud (2026-11-07).
- "Also included": Delivery lists "Postgres (incremental and exactly once), S3, Google Cloud Storage and local
  files". Spouts also deliver to Azure (`az://`, `abfss://`) and to a DuckLake (since 0.7.0). **[code]** for Azure
  Spouts; **[live]** DuckLake and mirror Spouts on Azure and Google Cloud (2026-11-07).
- Apps aren't mentioned. The "Pull orchestration" section's Tap sentence links `serving_apps_with_taps`; that link
  is about Taps, not Apps.
- Power BI: the README says "any Postgres client"; 0.8.0 makes Power BI's connector work (Npgsql 4.0.17).
  **[code]**: tested with the Npgsql versions Power BI bundles, replaying a logged Power BI session; Power BI
  itself not run. The Querying guide already has a Power BI section.
- Data roots on Azure and Google Cloud Storage are new in 0.8.0 (README doesn't list data roots at all today).
