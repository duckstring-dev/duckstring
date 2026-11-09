# "Running Duckstring on Databricks" guide: handoff

Facts for one guide, to be written in a fresh session (`docs/documentation_style.md` first). It's for a data engineer
whose platform is Databricks and who wants to run a Catchment there, knowing it's an unusual choice. Everything below
was built and measured on branch `apps` (2026-11-08/09) unless marked [unverified]. Sources: `plans/databricks-bigquery.md`
(parts 1 to 5 and the "As built" sections), the code named, and the live scripts kept in the session scratchpad.

Suggested place: Guides → Operating Catchments, beside Hosting on a Platform, which links to it from its Databricks
mention (`hosting_on_a_platform.md` line 8). The per-feature reference stays where it is (`formats.md`
`databricks://` and Change files, `cli/spout.md`, `cli/duck.md`, `environment.md`); the guide links to it. Pages still to
write that this guide would link to: the Python reference for `duckstring.databricks` and `duckstring.landing` (see
`plans/databricks-bigquery.md` "Docs handoff").

## What the guide should be honest about (the author: "somewhat ill advised", 2026-11-09)

- **Size.** Databricks Apps come in Medium (2 vCPU / 6 GB, 0.5 DBU/h, the default), Large (4 / 12, 1 DBU/h) and
  XLarge (12 / 48, 3 DBU/h, some regions) [Databricks docs]. Measured on Medium: `os.cpu_count()` 4, a cgroup v1
  memory limit of 8 GiB (the machine reports 15.5 GB). Every Duck runs inside the App as a subprocess, so the whole
  pipeline shares that. The memory budget reads the cgroup limit (`_memory_budget`), so with the defaults it is
  80% of 8 GiB less 2 GB, split into 4 slots.
- **Databricks' own guidance** discourages processing data in an App (warehouses and jobs instead) [Databricks docs].
- **Ducks can't run as Databricks jobs.** Not built (`plans/databricks-bigquery.md` "Not in this release"). Heavy
  recomputes can go to the SQL warehouse through the Flock (below).
- **Warehouse cost.** Each `databricks://` Spout delivery runs statements on the SQL warehouse (a run of two tables:
  4 statements, about 7.5 s), so a frequent recurring Refresh keeps a serverless warehouse awake (it auto-stops
  after its idle time, 10 min on the test warehouse). Observed on the test account: a day of Spout, Flock and reader
  testing on a 2X-Small serverless warehouse was 9.74 Serverless SQL DBUs, AUD 13.14 (2026-11-08). Change files (below)
  don't touch the warehouse.
- **One HTTP port.** An App gets one port behind Databricks' sign-in, so the Postgres wire and Flight serving
  surfaces aren't reachable (plan part 5). Queries through the HTTP API and the web UI work.
- **Restarts.** Local disk survives a redeploy and is lost on a compute stop/start (measured), so the data root and
  state backup go on a Volume (below).

## Deploying the Catchment as an App

Measured setup (`dbx/app/` and `appctl.py` in the scratchpad): the App's source folder held `app.yaml`, a `start.py`
and `requirements.txt`; it was uploaded to a workspace folder with the Workspace API and deployed with the Apps API
(`POST /api/2.0/apps/{name}/deployments`, `source_code_path`, `mode: SNAPSHOT`). The UI's equivalent is creating a
custom App and deploying from a workspace folder [standard Databricks flow; the test used the API only].

- **Command.** The test's `start.py` (after a probe) set three variables and exec'd
  `python -m uvicorn duckstring.catchment.asgi:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT --workers 1`. One worker,
  always (one Catchment process). Databricks injects `DATABRICKS_APP_PORT` and also `UVICORN_PORT` and `UVICORN_HOST`
  (measured), so `command: ["uvicorn", "duckstring.catchment.asgi:app", "--workers", "1"]` in `app.yaml` with the
  variables in its `env:` should be enough without a start script [unverified: run it in the release smoke test].
- **app.yaml env used:**
  - `DUCKSTRING_STATE_ROOT`: local disk. The test used `/home/app/.duckstring` (home; `/local_disk0` doesn't exist in
    an App; `/home/app`, `/tmp` and the source folder are writable, one 184 GB disk with 160 GB free).
  - `DUCKSTRING_DATA_ROOT=/Volumes/<catalog>/<schema>/<volume>/catchment/data`
  - `DUCKSTRING_STATE_BACKUP_URI=/Volumes/<catalog>/<schema>/<volume>/catchment/state`
  - `DUCKSTRING_CHECKPOINT_INTERVAL=15s` (the database backup cadence; given the shutdown window below).
  - `DUCKSTRING_CATCHMENT_NAME` (optional, the name shown in the UI).
  - No `DUCKSTRING_API_KEY`: Databricks' sign-in gates every request.
- **requirements.txt.** The test installed this branch's wheels by relative path plus `sqlglot` and `fsspec`; for a
  release that is `duckstring[databricks]` (the extra is `sqlglot` and `fsspec`; httpx and pyarrow are core). On Azure,
  the Flock's and Spout's Entra fallback needs `azure-identity` (the `azure` extra), but an App doesn't need it: it
  is given its own OAuth client (`DATABRICKS_CLIENT_ID`/`_SECRET`), which `databricks_rest` uses first after a token.
  PyPI was reachable from the App (measured) [installing from PyPI on an App: unverified, do it in the RC smoke test].
- **Injected environment** (measured): `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`,
  `DATABRICKS_APP_PORT`, `DATABRICKS_APP_NAME`, `DATABRICKS_APP_URL`, `DATABRICKS_WORKSPACE_ID`, `UVICORN_HOST`,
  `UVICORN_PORT`. Ubuntu 22.04, Python 3.11, uv; no root.
- **Ducks** run on the App's machine and dial back to the Catchment; the address is learned from the first request,
  nothing to set (measured: the Trickle demo ran end to end).
- **Region.** Apps aren't offered in every region (Australia Southeast answered 503 TEMPORARILY_UNAVAILABLE; the test
  moved to Australia East). Point at Databricks' region table.

### The App's service principal

Each App has its own service principal (shown in the App's settings). It needs, granted by someone who can grant:
- on the Volume holding the data root and state backup: `READ VOLUME`, `WRITE VOLUME` (with `USE CATALOG`,
  `USE SCHEMA`);
- for a `databricks://` Spout: on the target schema `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE`, `MODIFY`, `SELECT`,
  and `READ VOLUME`/`WRITE VOLUME` on the staging Volume; **Can use** on the SQL warehouse;
- for the Flock on Databricks SQL: the same as the Spout (it stages in a Volume and runs on the warehouse);
- for reading tables (below): `USE CATALOG`, `USE SCHEMA`, `SELECT`, plus Can use on a warehouse for the fallback,
  and `EXTERNAL USE SCHEMA` for the storage route.
These are what the test identities held (`plans/databricks-bigquery.md`, Databricks measured, Part 5).

### Storage on a Volume

- `/Volumes` isn't mounted inside an App (measured: the path doesn't exist and `/` isn't writable). Duckstring reaches a
  `/Volumes/...` data root and state backup through the Databricks Files API instead (`volumes.py`,
  `storage.VolumeStorage`; automatic where `/Volumes` is missing; `DUCKSTRING_VOLUMES=api|mount` overrides).
- Every read uses a copy on the App's own disk; at start the Catchment restores any line whose local copy is missing
  or older than the Volume's (`dataplane.restore_published`). So the Volume is the durable copy and the disk the
  working one.
- Measured: the Trickle demo persisted to the Volume (12 files, 36 MB with the lease and state backup) and came back
  whole after a compute stop/start, with the next refresh running on. A compute stop took 41 s and a start 122 s; a
  clean stop's full state checkpoint took 102 s (it writes `state.tar`).
- An App gets 15 s between SIGTERM and SIGKILL [Databricks docs], so a large Catchment's shutdown checkpoint may not
  finish and the previous `state.tar` stays. Since 2026-11-09 a Duck whose restored registry is behind the published
  data rebuilds it (`a806b442`), so that costs a rebuild from published state, not a wrong answer. Hence the short
  checkpoint interval.
- Files API facts worth one sentence at most: a file being uploaded is visible but empty for the whole upload, and an
  overwrite blanks it; Duckstring writes through a shadow file so readers never see that (`VolumeFileSystem`).

## Reaching it

- **Web UI:** the App's URL, through Databricks' sign-in. Users need **Can use** on the App (the test granted the
  `users` group with the permissions API).
- **CLI:** Databricks' sign-in refuses an Entra token (401) and accepts a Databricks OAuth token as a Bearer
  (measured with an M2M token for a service principal, `/oidc/v1/token`, scope `all-apis`; it lasts an hour). Since
  2026-11-09: `duckstring catchment connect --name dbx --path <App URL> --header-command 'Authorization: databricks auth
  token --host <workspace URL>'`, which runs the command for the token and again before it expires
  (`config.command_header`; JSON with `access_token` becomes `Bearer <token>`). [unverified: a person's U2M token from
  `databricks auth token` against an App; only M2M was measured.] For a service principal, a small script minting the
  M2M token works as the command.
- **App logs:** `GET {App URL}/logz/batch` with the same token returns the log as JSON (measured).

## Data in from Databricks (`duckstring.databricks`, `delta_log.py`)

Two calls in Ripple code, each returning an `Input` with `version`, `changed`, `via`, `read()` and `added()` (and
`NotAppendOnly`). The guide's recipes: `if not inp.changed: pond.skip()`; `added()` with `append_table`;
`read()` with `merge_table`. Check `changed` before `added()`: on the warehouse an unchanged `added()` still runs a
statement.

- **Files written to a Volume** (`databricks.files(pond, "/Volumes/.../path")`), expected to be the common route (the
  author, 2026-11-09): a Databricks job writes Parquet or Delta there with any mode, unchanged. Delta is read as Delta
  (deletion vectors handled); Parquet uses Spark's commit markers, so a job still writing isn't read. Through the Files
  API in an App the files are copied to the App's disk (`DUCKSTRING_INPUT_CACHE`, default the temp dir), fetching only
  new files. Measured from a laptop: a Delta table with deletion vectors opened in 1.6 s and read in 1.1 s cold;
  Parquet 1.2 + 0.4 s; partitioned Parquet 3.4 + 1.8 s; an unchanged next run 0 to 30 ms. Real Spark jobs between runs:
  appends gave exactly the new rows, also across an `OPTIMIZE`; a DELETE raised `NotAppendOnly`.
- **Catalog tables** (`databricks.table(pond, "catalog.schema.table", warehouse=None)`): the version comes from Unity
  Catalog's table info (about 50 ms, no warehouse). The data comes straight from storage with credentials Unity Catalog
  vends, when the metastore allows it, else through the SQL warehouse (`warehouse=`, `DUCKSTRING_DATABRICKS_WAREHOUSE`,
  `DATABRICKS_WAREHOUSE_ID`). Views, non-Delta tables and catalog-managed-commit tables always use the warehouse.
  Measured on the warehouse: first read 2.4 s, unchanged 70 ms with no statement, an insert gave the 25 new rows, an
  update raised `NotAppendOnly`. **The storage route has not run against a real metastore** (the test metastore has
  external data access off and the author shipped without it, 2026-11-09); it is tested offline against a local Delta
  table. Say so. Enabling it: a metastore admin turns on External data access (Catalog → gear → Metastore → Details),
  and the reading principal needs `GRANT EXTERNAL USE SCHEMA ON SCHEMA c.s TO ...`, which only the catalog owner can
  grant and `ALL PRIVILEGES` doesn't include [Databricks docs]. Note for readers on personal Azure accounts: becoming
  metastore admin goes through the account console (accounts.azuredatabricks.net), which refuses a personal Microsoft
  login ("does not exist in tenant 'Microsoft Services'"); a work account in the directory is needed (seen 2026-11-09).
- Not built: CSV/JSON files, GCP credential vending (falls back to the warehouse), cache retention.

## Data out to Databricks

- **`databricks://` Spout** (`egress/databricks.py`): Unity Catalog tables kept up to date by the warehouse. Merge
  Trickle → one `MERGE` by key; append Trickle → `INSERT ... REPLACE WHERE _duckstring_f = ...` with a `_duckstring_f`
  column kept in the table; plain table → `INSERT OVERWRITE`. Exactly once across a crash (watermark table
  `_duckstring_egress` in the schema). Staged through a Volume, so no external location. Destination
  `databricks:///catalog.schema?warehouse=<id>&volume=/Volumes/...` (host from `DATABRICKS_HOST`, which an App has).
  Measured in an App: `revenue_by_product` (100,000 rows) and `priced_line` (1,525,000) delivered, counts matching.
  Timing: about 7.5 s and 4 statements per run of two tables, after the speed work (18.5 s and 16 before).
  The web UI's Spout form offers it since 2026-11-09.
- **Change files** (`--mode changes`, any object store, `file://` or `/Volumes/...`): per delivery, per table, a file of
  the changes plus a manifest; no warehouse at write time. Measured: 1.3 to 2.0 s per delivery from a laptop to a
  Volume. Readers apply them on their own schedule: in Databricks with `read_files(..., schemaEvolutionMode => 'none')`
  and a `MERGE` (one per delivery, or all at once with a newest-per-key `QUALIFY`), measured to end equal to the
  source; Databricks reads `_duckstring_op` as tinyint and `_duckstring_f` as timestamp. The layout is in `formats.md`
  ("Change files"). Auto Loader can't watch the folder (files are visible, empty, while uploading), so readers follow
  manifests. Not built: retention of delivered files, a PySpark reader.
- Choosing: the Spout when the tables should simply be in Unity Catalog and warehouse time is acceptable; change files
  when the warehouse cost matters or a job already runs on a schedule.

## Heavy recomputes on the SQL warehouse (the Flock)

- `duckstring duck set POND --flock upgrade --engine databricks` (or `[flock]` in `pond.toml`), with
  `DUCKSTRING_FLOCK_DATABRICKS_WAREHOUSE` and `DUCKSTRING_FLOCK_DATABRICKS_VOLUME` (the staging Volume);
  `DUCKSTRING_FLOCK_DATABRICKS_HOST` defaults to `DATABRICKS_HOST`, which an App has, and `_TIMEOUT` to 900 s
  (`flock/engines/databricks.py`). Reference rows in `environment.md` and `cli/duck.md` are done.
- What goes: a Trickle terminal's wholesale recompute (bootstrap, coverage miss, an over-threshold change, `.sql()`
  terminals), never an incremental run. DuckDB stays the authority: results are checked against the job's schema and,
  on SQL engines, sample-verified; anything refused runs locally.
- Measured: about 1.2 s per dispatch on a warm 2X-Small serverless warehouse, 12.7 s for the first. Constructs that
  differ on Databricks run locally (`floor`/`ceil` on DOUBLE, `date_trunc`, `median`, `strftime`, `//` on DECIMAL).
  The measured list is in the module; the guide shouldn't repeat it.
- Inside an App this is the main way to get more compute than the App's machine.

## Open items for the release candidate

- Deploy the RC from PyPI to the test App with a plain `app.yaml` (no `start.py`) to confirm the command and
  `requirements.txt` the guide will show. The App `duckstring` on the hybrid workspace is stopped (AUD 0 while
  stopped); its source is in the test SP's workspace folder `duckstring-app`.
- Try `--header-command 'Authorization: databricks auth token ...'` with a person's login against the App, if the
  author can (needs the Databricks CLI signed in as a workspace user).
