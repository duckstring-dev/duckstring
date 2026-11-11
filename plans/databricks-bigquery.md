# BigQuery and Databricks: Flock engines, a Databricks Spout, Catchments on Databricks Apps

**Status: parts 1 to 4 built (2026-11-08), BigQuery and Databricks (Flock and Spout) measured live; part 5 (the Catchment as a Databricks App) not yet tried. For 0.8.0.** See "As built" at the end. Decided in conversation with the author on 2026-11-08. Each fact
is tagged as on the Azure/Google plan: **[code]** read in this repo, **[docs]** from the vendor's docs,
**[unverified]** believed but not checked, **[decision]** the author's call.

**Goal.** A Flock engine for every cloud, so no cloud is the preferred one: BigQuery for Google, Databricks
SQL for Azure, and Databricks on every cloud for teams already on it. Then make the Databricks story complete
for a team that wants to cut its Databricks costs: Duckstring output lands in Unity Catalog through a Spout,
and a Catchment can run as a Databricks App inside a workspace IT has locked down.

**Decided against** [decision]:
- **A Fabric Flock engine.** The Fabric Warehouse has no documented way to write a result as files, needs a
  running capacity, and speaks T-SQL, the hardest dialect to translate. Fabric users are guided to move data
  in and out instead (Spouts and Inlets, below) and to use another engine if they need a Flock.
- **Unity Catalog as the data plane.** The October reasoning (`plans/data-plane-choice.md`) holds: Trickle
  tiers and versioned overwrite tables aren't table-format tables, it would put an external service in the
  path of every Run, and it duplicates what the Catchment already tracks. Unity Catalog belongs at the edges:
  a Spout now, an Inlet later.
- **App scale-out** is a separate release (0.9.0).

## The user story this serves

A data engineer has a Databricks workspace in which they can create schemas, Volumes, SQL warehouses, jobs and
Apps, but not external locations, storage credentials or metastore settings. They host a Catchment as a
Databricks App, read their existing Unity Catalog tables into Ponds, and send the Ponds' output back to Unity
Catalog through Spouts. Jobs too large for the App's machine go to a SQL warehouse through the Flock.

Everything below works within those permissions. In particular nothing requires an external location: the
Flock and the Spout stage files in a **Volume**, and a SQL warehouse reads them with `read_files`.

## Part 1: the BigQuery Flock engine

A `SqlEngine` subclass (`flock/engines/sql_engine.py` [code]): the base stages each leaf as Parquet under the
engine's scratch prefix (`stage_select`), substitutes each leaf's `read_of` into the translated SQL, calls
`execute` (which writes the result as Parquet under `out/`), and reads it back with DuckDB. Athena
(`engines/athena.py`) is the closest template.

- **Scratch.** `DUCKSTRING_FLOCK_BIGQUERY_SCRATCH`, a `gs://` prefix, written by the Duck through the data
  plane's storage (Google credentials as for a `gs://` data root, `storage.duckdb_secret_sql` [code]).
- **Reads.** Each leaf as a temporary external table over the staged Parquet in a scratch dataset
  (`DUCKSTRING_FLOCK_BIGQUERY_DATASET`), dropped in `cleanup`, as Athena does with Glue tables. The other
  option is `LOAD DATA` into a native table (loading is free, querying it is then faster); measure both.
- **Writes.** `EXPORT DATA OPTIONS (uri = '{out}/*.parquet', format = 'PARQUET', overwrite = true) AS …`
  [unverified syntax details].
- **Client.** REST through google-auth's `AuthorizedSession`, as `gcp_rest.py` does for Cloud Run [code]:
  `jobs.insert` and `jobs.get` on `bigquery.googleapis.com/bigquery/v2`. No new dependency in the `gcp` extra.
- **Config.** `DUCKSTRING_FLOCK_BIGQUERY_{PROJECT,DATASET,LOCATION,SCRATCH}`. The project defaults to the
  credentials' project. Enabled when dataset and scratch are set.
- **Dialect.** sqlglot's `bigquery` dialect. Known differences to measure, not assume [unverified unless
  noted]:
  - `/` returns FLOAT64 and raises on a zero divisor; `IEEE_DIVIDE` returns ±inf and NaN, which may serve
    as the division rewrite StarRocks needed.
  - A single INT64 and a single FLOAT64 type. `conform` casts results back to DuckDB's types [code], so
    this only matters where a value differs.
  - NUMERIC is DECIMAL(38,9) and BIGNUMERIC wider, so DECIMAL result scales can differ from DuckDB's.
  - NULLs sort first in ascending order (DuckDB: last). This matters for `.sql()` queries with ORDER BY
    plus LIMIT, or window functions.
  - TIMESTAMP is an instant and DATETIME a wall-clock time. Staged TIMESTAMPTZ columns are UTC wall-clock
    TIMESTAMPs (`stage_select` [code]), so check which BigQuery type the staged Parquet arrives as.
  - Strings compare by code point by default, which matches DuckDB.
- **Profile.** Start as narrow as Athena's measured list and widen it only by measurement:
  `tests/test_flock_bigquery_conformance.py`, run against the Google test project (skipped without
  credentials), following the StarRocks conformance test's report format.
- **Cost.** On-demand queries are billed per TiB scanned, with the first TiB each month free [docs]; the
  conformance runs fit inside that.

## Part 2: a Databricks client shared by the Flock and the Spout

One small module, `databricks_rest.py` (alongside `gcp_rest.py`), used by both parts below.

- **Auth**, in this order:
  1. OAuth machine-to-machine with a Databricks service principal (`DATABRICKS_CLIENT_ID` and
     `DATABRICKS_CLIENT_SECRET`, token from `{host}/oidc/v1/token`). Works on every cloud, and it's what a
     Databricks App injects for its own service principal [docs].
  2. On Azure, an Entra token from `azure-identity` for the Azure Databricks resource
     (`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`, the same in every Azure environment [docs]), so a Duck with a
     managed identity needs no stored secret.
  3. A personal access token (`DATABRICKS_TOKEN`) as the fallback.

  Secrets through the secret store as for other engines (`SECRETS`, `engine_secrets` [code]).
- **Statements.** The Statement Execution API (`POST /api/2.0/sql/statements` with `warehouse_id`,
  `wait_timeout`, then polling). Results either inline or as `EXTERNAL_LINKS` in `ARROW_STREAM` format:
  Arrow chunks downloaded from presigned links [docs; unverified whether a locked-down network allows the
  links].
- **Files.** The Files API for Volumes (`PUT`/`GET`/`DELETE /api/2.0/fs/files/Volumes/...`) [docs;
  unverified: the size limit of a single PUT, and whether a multipart upload is needed for large leaves].
- **Dependency** [decision pending]: plain httpx REST (httpx is already a dependency), as with `gcp_rest.py`,
  or `databricks-sdk` in a new `databricks` extra. Only a handful of endpoints are used, which favours REST;
  the SDK would bring its unified auth (CLI profiles, Azure CLI, metadata service).

## Part 3: the Databricks SQL Flock engine

A `SqlEngine` subclass that stages to a Volume instead of an object-store prefix.

- **Scratch.** `DUCKSTRING_FLOCK_DATABRICKS_VOLUME` (`/Volumes/{catalog}/{schema}/{volume}/...`). The Duck
  writes each leaf to a local temporary Parquet file and uploads it through the Files API, so it works from
  any Duck, inside Databricks or not. `_run`'s staging (`COPY ... TO stage.uri(...)`, [code]) needs a hook
  for this; the base assumes DuckDB can write the scratch location directly.
- **Reads.** `read_files('/Volumes/.../src{i}', format => 'parquet')` [docs], or a `parquet.`path``
  reference; measure which keeps the staged types.
- **Results.** Fetched as Arrow through the Statement Execution API and written into a temp table, instead of
  the base's "engine writes Parquet under `out/`". Needs a second hook in `_run`. Writing to a Volume with
  `INSERT OVERWRITE DIRECTORY` is the alternative [unverified on SQL warehouses].
- **Config.** `DUCKSTRING_FLOCK_DATABRICKS_{HOST,WAREHOUSE,VOLUME}`, with auth as in Part 2.
- **Dialect.** sqlglot's `databricks` dialect. SQL warehouses run ANSI mode [unverified default]: integer
  overflow and division by zero raise, `/` returns a fractional type, strings compare as UTF-8 bytes by
  default, NULLs sort first in ascending order. Same approach as BigQuery: a narrow measured list first,
  `tests/test_flock_databricks_conformance.py` against the test workspace.
- **Warehouse choice.** Recommend a serverless SQL warehouse: it starts in seconds and stops when idle. A
  classic warehouse can take minutes to start, longer than most jobs worth dispatching [unverified figures].
- **Engine order** [decision pending]: today `ENGINE_ORDER = ("motherduck", "starrocks", "athena")` [code].
  The new engines need a place; one option is the DuckDB engines first, then by how wide each measured
  profile is.

## Part 4: the Databricks Spout

The destination teams on Databricks need, since their dashboards, notebooks and permissions all look in Unity
Catalog. Built on Part 2. Destination `databricks://{host}/{catalog}.{schema}?warehouse=...&volume=...`
[decision pending on the URI shape].

- **Contract** (`egress/base.py` [code]): the worker applies deltas only to a driver with
  `supports_delta and transactional` (`egress_worker.py:90` [code]); a transactional driver owns its
  watermark in the destination, and the worker skips a table whose watermark already covers `f`.
- **Delivery.** Stage the changes (upserts and deletes) as Parquet in the Volume, then on the warehouse:
  `MERGE INTO target USING read_files(...) ON <pk>` deleting removed keys and replacing changed rows. A full
  write is `CREATE OR REPLACE TABLE ... AS SELECT * FROM read_files(...)`. `ensure` creates the table, and a
  new source column becomes `ALTER TABLE ... ADD COLUMN`, as `AttachedCatalogDriver` does [code].
- **The watermark problem.** Postgres writes the table and the watermark in one transaction [code]. A Delta
  MERGE commits one table, so the watermark (`_duckstring_egress` in the target schema) is written after it.
  A crash between the two replays the window:
  - A keyed MERGE of the same changes gives the same result, so merge tables are safe.
  - An append table has no key, so a replay would insert its rows twice. Options: Databricks multi-statement
    transactions [unverified availability on SQL warehouses], or keep a run stamp column in the destination
    and delete the window before inserting.
- **Credentials** as Part 2. `test_connection` runs `SELECT 1` on the warehouse and lists the Volume.
- **Tests.** Offline with a fake warehouse (DuckDB standing in, as `test_flock_sql.py` does for engines),
  plus a real-workspace run.

## Part 5: a Catchment as a Databricks App

**Already built** [code], from `plans/storage-decoupling.md` (June 2026):
- `catchment/asgi.py` is the platform entry. Hot state stays on local disk (`DUCKSTRING_STATE_ROOT`, for
  example `/local_disk0`).
- `DUCKSTRING_STATE_BACKUP_URI` pushes a `duck.db` snapshot on an interval (SQLite backup API, safe while in
  use) and a warm bundle of ledgers and registries on a clean shutdown, after the Ducks stop
  (`catchment/state_sync.py`). The Catchment restores from it at boot.
- `DUCKSTRING_DATA_ROOT` accepts `/Volumes/...` as a local path (`storage.py`, `tests/test_storage.py`).
- With a data root set, the Catchment's own Ducks publish to local disk first and Persist copies the files to
  the data root (`launcher.py:99-103`, `driver.py:612-617`), so the Volume receives only whole-file writes.
- `docs/docs/guides/hosting_on_a_platform.md` names Databricks Apps.

Nothing of this has run on a real Databricks App.

**Platform facts** [docs, 2026-11-08]:
- Sizes: Medium 2 vCPU / 6 GB (default, 0.5 DBU/h), Large 4 / 12 (1 DBU/h), XLarge 12 / 48 (3 DBU/h, some
  regions).
- The local filesystem is lost on restart. An App gets 15 s after SIGTERM before SIGKILL.
- Injected env: `DATABRICKS_APP_PORT` (and `UVICORN_PORT`), `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`,
  `DATABRICKS_CLIENT_SECRET`. Ubuntu 22.04, Python 3.11, uv. No root, no `apt-get`.
- OAuth sign-in in front of every App; the App has its own service principal.
- Databricks' guidance discourages processing data in an App (warehouses and jobs instead).
- Australia East supports serverless SQL warehouses, serverless workspaces and Apps (the region table).

**To build:**
1. **Memory budget from the container limit.** `_memory_budget` reads physical memory with `os.sysconf`
   (`driver.py:91-101` [code]), which in a container can report the host's memory. Take the lower of that and
   `/sys/fs/cgroup/memory.max` (cgroup v2; `memory.limit_in_bytes` on v1). Also fixes Catchments on Container
   Apps and Cloud Run.
2. **Optional: a header from a command**, re-run when the token expires, for the CLI behind Databricks
   sign-in (`databricks auth token`) and a private Cloud Run service (`gcloud auth print-identity-token`).
   Today `catchment connect --header` takes a fixed value [code].
3. **Optional: a Unity Catalog read helper** in Ripple code: run a SELECT on a warehouse through Part 2's
   client and return a DuckDB relation, using the App's injected credentials. Without it, a Ripple can use
   `databricks-sql-connector` itself, and that is the documented recipe.
4. **Docs.** A Databricks section in Hosting on a Platform: `app.yaml` (`uvicorn duckstring.catchment.asgi:app`,
   one instance), the environment variables (state root on local disk, data root and state backup on a
   Volume, `DUCKSTRING_CHECKPOINT_INTERVAL=15s` given the shutdown window), the Volume grants for the App's
   service principal, and what doesn't work there (one HTTP port, so no Postgres wire or Flight).

**To verify on a real App:**
- Whether `/Volumes` is mounted inside an App. The author's fractfs Databricks recipe assumes it; the
  Databricks docs don't say [unverified].
- Whether a rename on the Volume replaces the file atomically. Persist writes each table's sidecar last, and
  the state backup replaces its objects, so this decides whether a reader can see a half-written file.
- State backup and restore across a redeploy, end to end.
- How much local disk an App gets.
- Whether the CLI can reach an App with a bearer token.
- What outbound access a restricted workspace allows (PyPI for Pond environments; DuckDB extension
  downloads, though a local data root needs none).
- That the Catchment learns a usable dial-back address for its own Ducks from the first request
  (`tests/test_platform.py` covers this generically [code]).

## Test accounts

**Google.** The existing project `duckstring-test-gcloud` (reference memory): enable the BigQuery API, create
a scratch dataset in `australia-southeast1`, grant the test service account BigQuery Job User and Data Editor
on the dataset. Scratch under the existing bucket.

**Azure Databricks**, in the existing subscription and resource group `duckstring-test` (australiaeast).
Azure Databricks is billed by Microsoft on the Azure invoice, so the subscription's credits should cover it
unless the credit offer excludes it [unverified for this offer]. Serverless SQL and Apps run on Databricks'
compute and bill as DBUs, with no VMs in the subscription.
1. **The author** creates the workspace in the portal (the first workspace creates the Databricks account,
   and its creator becomes account admin; the test service principal is Owner on the resource group only and
   can't register providers). Premium tier, or "Trial (Premium, 14 days free DBUs)", which the docs say isn't
   available on an Azure free-trial subscription. Registering `Microsoft.Databricks` may be needed first.
2. Add the test service principal (`duckstring-test-claude`) to the workspace as an ordinary user, with
   OAuth M2M or Entra tokens. To recreate the user story, don't make it a workspace or metastore admin.
3. A serverless SQL warehouse (2X-Small, auto-stop after 10 minutes), `CAN_USE` for the service principal.
4. A catalog or schema the service principal can create tables in, and a Volume with `READ VOLUME` and
   `WRITE VOLUME`.

From there everything can be driven with the test service principal's credentials from the existing env file.

## Order of work

1. The memory budget fix (Part 5, item 1).
2. BigQuery engine and its conformance run (Part 1), which needs nothing new from the author beyond enabling
   the API.
3. The Databricks client (Part 2), then the Flock engine (Part 3) and the Spout (Part 4), offline first.
4. With the workspace: the conformance run, the Spout run, then a Catchment as an App running the Trickle demo
   with a Databricks Spout, a restart, and a redeploy.
5. Docs: the Flock reference and AWS/Azure/Google guide sections for the new engines, a Databricks Spout
   section in the Spouts guide, and the Databricks Apps section above.

## Not in this release

- A Unity Catalog Inlet as a feature (the read helper or a recipe covers 0.8.0).
- Ducks as Databricks Jobs on serverless compute (a backend in the provider seam, like Container Apps jobs).
- A Fabric Spout through open mirroring (Parquet change files in a landing folder that Fabric applies to a
  Delta table [unverified format]).
- Writing to Unity Catalog directly from DuckDB (Iceberg REST or Delta writes), which would avoid warehouse
  time but needs a spike on how mature those writes are.

## Sources

- [Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/),
  [environment](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/system-env),
  [compute sizes](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/compute-size),
  [key concepts](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/key-concepts),
  [best practices](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/best-practices)
- [Azure Databricks features by region](https://learn.microsoft.com/en-us/azure/databricks/resources/feature-region-support)
- [Azure Databricks free trial](https://learn.microsoft.com/azure/databricks/getting-started/free-trial)
- [Work with files in Unity Catalog volumes](https://docs.databricks.com/aws/en/volumes/volume-files)
- Fabric (for the decision against): [OPENROWSET GA](https://blog.fabric.microsoft.com/en-US/blog/the-openrowset-function-is-generally-available-in-fabric-data-warehouse/),
  [collation](https://learn.microsoft.com/en-us/fabric/data-warehouse/collation),
  [data types](https://learn.microsoft.com/en-us/Fabric/data-warehouse/data-types)

## As built (2026-11-08)

**Code.**
- `flock/engines/bigquery.py` (`BigQueryEngine`), `flock/engines/databricks.py` (`DatabricksEngine`),
  `databricks_rest.py` (the shared client: auth, statements, Arrow chunks, Volume files), `egress/databricks.py`
  (`DatabricksEgressDriver`, scheme `databricks`). Registered in `flock._BUILTIN_ENGINES` and
  `egress.base._register_builtins`; `databricks` joined `destination.LAKE_SCHEMES` (any source, no pk needed).
- `ENGINE_ORDER = ("motherduck", "starrocks", "databricks", "bigquery", "athena")`: the new engines sit after
  StarRocks and before Athena, whose measured list is the narrowest. The author can reorder.
- `SqlEngine.STAGE_FILE_BYTES` (default `None`): an engine that sets it gets each source staged as a directory of
  `data_{n}.parquet` files (DuckDB `FILE_SIZE_BYTES`); Databricks sets 1 GiB because a Files API upload is limited
  to about 5 GiB [forum report].
- The Databricks engine stages on local disk and uploads in `read_of`, and `execute` writes the Arrow result
  chunks as Parquet locally, so the base `_run` needed no other hook. An empty result is written from the
  statement manifest's schema.
- Extras: `gcp` gained sqlglot (BigQuery translates jobs); a new `databricks` extra is sqlglot alone (httpx and
  pyarrow are core). On Azure the Entra path needs `azure-identity` from the `azure` extra.
- `_memory_budget` takes the lower of physical memory and the cgroup limit (`memory.max`, v1
  `memory.limit_in_bytes`); `reference/environment.md` says so.
- The conformance battery moved to `tests/flock_conformance.py`, shared by StarRocks (unchanged behaviour; its CI
  job is the check), BigQuery and, later, Databricks.

**The append-table question (Part 4) is settled:** an append Trickle's window goes in as `INSERT INTO t (cols)
REPLACE WHERE _duckstring_f = '<f>' SELECT ...`, with the delivery's freshness kept in the destination as an ISO
string column `_duckstring_f`, so a replay replaces its own rows. A merge Trickle's window is one `MERGE` whose
source holds the changed rows (`__ds_op = 'u'`) and the removed keys not re-added (`'d'`), matched with `<=>`. A
plain table is `INSERT OVERWRITE t (cols) SELECT ...`. The table is created with `CREATE TABLE IF NOT EXISTS ... AS
SELECT ... LIMIT 0` from the staged files (types as the warehouse reads the Parquet), new columns with `ALTER TABLE
... ADD COLUMNS` typed from `DESCRIBE QUERY`. An empty window skips the warehouse and only moves the watermark.
All of this runs against a fake workspace offline (`tests/databricks_fake.py`), and was run live on 2026-11-08
(below).

**BigQuery, measured 2026-11-08** (australia-southeast1, sqlglot 30.22; dataset `duckstring_flock` with a 1-day
default table expiration, scratch `gs://duckstring-test-gcloud-data/flock-scratch`). First report: arithmetic, all
three zero-division cases (through `IEEE_DIVIDE`), rounding, CASTs to integers, strings (including non-ASCII case
mapping), the aggregates and most windows agreed. Fixed from the report:
- `year/month/day/quarter` aren't BigQuery functions, `dayofweek` became `DAY_OF_WEEK`: rewritten as `EXTRACT`
  (Sunday is 1 in BigQuery, 0 in DuckDB).
- `ln`/`log` of a DECIMAL came back as NUMERIC (nine places): arguments cast to DOUBLE.
- BigQuery reads a staged naive Parquet timestamp as TIMESTAMP (an instant), so a comparison with a DATETIME
  literal failed: `read_of` wraps the external table in `SELECT * REPLACE (DATETIME(c) AS c)`.
- Kept out: `CAST(DECIMAL AS VARCHAR)` (trailing zeros dropped), `first_value` (NULLS LAST refused in a RANGE
  frame), `% ` on DOUBLE, `median`, `regr_*`, ordered `string_agg`. Each dispatch took 3 to 5 s and was billed at
  BigQuery's 10 MB minimum.

**Databricks, measured 2026-11-08** (Azure Databricks workspace `adb-7405616522734253.13`, Australia Southeast,
Premium, a serverless workspace; catalog `ds_test_dbx`, schema `duckstring_test`, Volume `staging`; a 2X-Small serverless SQL warehouse,
Databricks SQL 4.2.0; sqlglot 30.22). Signed in as the Entra service principal `duckstring-test-claude` with an
Entra token (no Databricks secret), with workspace access, Databricks SQL access, Can use on the warehouse, and on
the schema `USE CATALOG`, `USE SCHEMA, CREATE TABLE, MODIFY, SELECT, READ VOLUME, WRITE VOLUME`: enough for both
the Flock and the Spout.
- Found live: `read_files` adds a `_rescued_data` column by default, which the Spout created in every delivered
  table. Both read sites pass `schemaEvolutionMode => 'none'`, and the fake workspace adds the column unless that
  option is given (the offline Spout tests fail without the fix).
- Flock report: arithmetic, all three zero-division cases (through the CASE rewrite), CASTs to integers (the
  `round`/`bround` rewrite), strings, the aggregates (the statistics within float_order), `regr_slope/sxx/count`,
  ordered `string_agg` and every window agreed. Fixed: `dayofweek` (Sunday is 1) rewritten as `dayofweek - 1`.
  Kept out: `floor`/`ceil` on DOUBLE (a clamped BIGINT; with a scale, a DECIMAL overflow), `//` on DECIMAL (the
  int_only rule), `date_trunc` (returns an instant from a naive timestamp or a date, so time-zone dependent),
  `median`, `strftime`. Every builder shape agreed through conform and sampled verification (119 passed, 11 not
  sent). About 1.2 s per dispatch on a warm warehouse; the first took 12.7 s.
- Spout: the offline suite's scenarios against real Unity Catalog tables (`spout_live.py` in the session
  scratchpad): first delivery, a merge (changed, removed and added keys in one MERGE), an append, an overwrite,
  the watermark, a replay after a lost watermark changing nothing, a gained column, the Test probe. Each
  delivery's staging directory is removed; the empty `{volume}/.duckstring-staging/` parent stays.
- Databricks Apps aren't offered in Australia Southeast (Microsoft's feature-region table; the Apps API answered
  503 TEMPORARILY_UNAVAILABLE there), so part 5 runs on a second, Hybrid workspace in Australia East.

**Part 5, measured 2026-11-08** on a Hybrid workspace in Australia East (`adb-7405616089776615.15`, catalog
`hybrid_dbx`, warehouse `22f730191698ef19`). App `duckstring` (Medium), created and deployed by the test service
principal through the Apps API (source uploaded with the Workspace API to its home folder: `app.yaml`, a
`start.py` that probes then execs uvicorn on `duckstring.catchment.asgi:app`, and `requirements.txt` naming this
branch's two wheels by relative path, which pip installed from the App folder). The App's own service principal
needed the schema grants and Can use on the warehouse, given by the author (the test SP can't grant).
- **No `/Volumes` in an App** [measured]: the path doesn't exist and `/` isn't writable, so a `/Volumes/...` data
  root or state backup fails at boot (`Permission denied: '/Volumes'`). The plan's assumption (and the fractfs
  recipe's) was wrong. No `/local_disk0` either.
- Disk: `/home/app`, `/tmp` and the source folder are writable, one 184 GB disk (160 GB free). Kept across a
  **redeploy** (state, runs, Spouts all survived), lost on a **compute stop/start** (41 s to stop, 122 s to come
  back, `No Ponds deployed`).
- Memory: cgroup v1, `memory.limit_in_bytes` 8 GiB (the machine reports 15.5 GB; the cgroup fix applies).
  `os.cpu_count()` 4.
- Outbound: PyPI reachable; `github.com` timed out (504) [the extension host was probed with a bad URL; unknown].
- Injected env as documented, plus `DATABRICKS_APP_NAME`, `_APP_URL`, `_WORKSPACE_ID`, `UVICORN_HOST`.
- **CLI access**: the App's sign-in proxy refuses an Entra token (401) and accepts a Databricks OAuth M2M token
  (`/oidc/v1/token`, scope `all-apis`) as `Authorization: Bearer`, so `catchment connect --header` works (an hour's
  token). `/logz/batch` returns the App log as JSON with the same token.
- With everything on local disk: the Trickle demo deployed and ran end to end (Ducks as subprocesses; the
  dial-back address learned from the first request, nothing set), queries served, and two Databricks Spouts
  (`databricks:///hybrid_dbx.duckstring_test?...`, host and credentials from the App's injected env) delivered
  `revenue_by_product` (100,000 rows) and `priced_line` (1,525,000) to Unity Catalog, counts matching.
- **Files API as a store** [measured from a laptop]: ranged GET (206), HEAD (size, Last-Modified, no ETag),
  directory listing (404 for a missing directory), delete. A PUT replaces an existing file even without
  `overwrite=true` (no create-if-absent), and an overwrite isn't atomic for readers: 3 of 7 reads during 10
  overwrites returned a 200 with an empty body.
- **Built (2026-11-08): Volumes through the Files API** (`volumes.py`, `storage.VolumeStorage`; see CLAUDE.md Data
  plane). Where `/Volumes` isn't mounted a `/Volumes/...` data root and state backup go through the Files API;
  DuckDB never reads them: reads always take the local publish, which `create_app` restores from the Volume at
  start (`dataplane.restore_published`), eviction keeps it, Draws land locally. Live: the operations and a
  persist/restore round trip from a laptop (14 s); on the App, the Trickle demo persisted to the Volume (12 files,
  36 MB, the lease and the state backup) and came back whole after a compute stop/start, with Gen and the
  revenue total unchanged and the next refresh running on. A clean stop took 102 s (the full state checkpoint),
  so `state.tar` restored the local publishes itself; the Files API restore is the path for a Catchment that dies
  without one (not forceable through the Apps API).
- Also fixed: a Duck logged every job poll at INFO through httpx, which filled the App's log window (it keeps
  only the newest lines) and pushed out start-up; httpx is now at WARNING in the Duck, as the Azure SDK was.
- Open risk, not specific to Volumes: a `state.tar` older than `duck.db` (a shutdown killed mid-upload leaves the
  previous one) restores registries older than the published state, and a Duck only rehydrates a registry that's
  missing. The 15 s App shutdown window makes this likelier on a large Catchment. **Fixed 2026-11-09:** each
  export records its run's `f` in the registry (`dataplane.PUBLISHED_TABLE`, `_duckstring_published`, written
  before the sidecar commit; hydration records the sidecar's run stamp), and a Duck whose registry holds an older
  `f` than the published run stamp (local publish, else the persist layer) sets it aside as
  `registry.duckdb.behind-{stamp}` and rehydrates (`dataplane.registry_behind`, `executor._set_aside`). A registry
  with no record (empty, wiped by a Rebuild, or from before the fix) is never called behind. The dbt executor
  doesn't check. Test: `test_hydrate.test_executor_rebuilds_a_registry_older_than_its_published_state`.
- **CLI behind short-lived tokens (2026-11-09):** `catchment connect --header-command 'Name: command'`
  (`config.command_header`): the shell command's output is the value (JSON with `access_token`, as `databricks
  auth token` prints, gives `Bearer <token>`; a bare token for an `*Authorization` header gets `Bearer`), cached
  in `~/.duckstring/header-cache.json` (0600) until 5 min before its expiry (a JWT's `exp` or the JSON's
  `expiry`; unknown = run per request); `_http.request` re-runs it once on a 401. A Duct stores only the current
  value (warned). Not yet tried against a real Databricks App with `databricks auth token` (U2M); the App proxy
  was measured to accept an M2M OAuth token. Docs: `cli/catchment.md`, the Google guide's connect example.

**Change files, built 2026-11-09** (`mode=changes`; the author's design calls: performance before plain
integration, so a reader in Duckstring is fine; an op column only where there's a key).
- Measured first (from a laptop, hybrid workspace): a **new** file is visible from the moment its upload starts,
  listed and answering HEAD with size 0, a ranged read 400, for the whole upload (64 MiB in 10 s, 256 MiB in 40 s,
  6.3 MiB/s on a home uplink); an **overwrite** blanks the file for the whole upload (HEAD 0, empty body, the old
  content gone at once). Calls: PUT 2 KiB 230 ms, HEAD 120 ms, GET 140 ms, list 160 ms (medians).
- That overturned the shipped Volume store's retry, which assumed HEAD kept reporting a size during an overwrite,
  and meant a writer dying mid-overwrite of a sidecar (or `duck.db`, `state.tar`, the lease) left it empty.
  `VolumeFileSystem` now writes every non-Parquet file through a shadow (`.{name}.duckstring-next`: shadow, file,
  delete the shadow); a read finding the file empty takes the shadow, then reads the file once more (an upload that
  just finished) before calling it empty. Parquet is never shadowed (written once under a name a commit point
  names); an empty Parquet read is retried. Shadows are hidden from `ls`. `Storage.write_new` skips the shadow for
  a file written once (a manifest). Cost: two more calls per overwrite (a `state.tar` uploads twice).
- The Spout (`egress/landing.py` writer, `landing.py` layout and reader; formats.md "Change files" has the layout):
  per delivery, per table, the window as one file (chunked at 256 MiB) or the whole table (`.full`), then a
  manifest under `_duckstring/manifests/{day}/{hour}/`. A table with a pk: one row per changed key with
  `_duckstring_op` (+1 new row, -1 old row of a key removed and not re-added); without: the Z-set with
  `_duckstring_d`. `_duckstring_f` (the delivery's freshness) on every row. Positions order deliveries (`f`, plus
  `#n` for a repeat at the same `f`: a resync, whose files take `.n`), so a reader past `f` still sees a resync.
- Exactly-once: `_duckstring/latest.json` records `{"f": delivered, "pending": attempting}` before the first data
  file; the next delivery takes the pending one only if its manifest is whole (an empty one left by a dead writer
  is removed). A failure the worker sees removes that attempt's files. The delivered position is cached per
  process (`_KNOWN`), so a warm delivery reads nothing back: a Volume delivery is the `latest.json` write (three
  calls), one PUT per table (tables in parallel, `_PARALLEL_TABLES`) and the manifest.
- `/Volumes/...` (or `file:///Volumes/...`) is accepted as a `file` destination for every object-store mode;
  `get_storage` now resolves `file://` before the Volume check. `add_spout` refuses `changes` on other schemes.
- A plain (overwrite) table is republished by every Pond run, so it lands whole every delivery (as the Databricks
  Spout's `INSERT OVERWRITE` does); `write_table(if_changed=True)` holds an unchanged one.
- Live (`landing_live.py` in the session scratchpad): four deliveries of a merge and an append table to
  `/Volumes/hybrid_dbx/duckstring_test/staging/landing-live` from the laptop took 1.3 to 2.0 s each (the Databricks
  Spout: 7.5 s); the reader matched the source; the warehouse applied the files with `read_files(...,
  schemaEvolutionMode => 'none')` + `MERGE` (one per delivery, and all at once with a newest-per-key `QUALIFY`),
  ending equal to the source. Databricks reads `_duckstring_op` as tinyint, `_duckstring_f` as timestamp.
- Not built: retention of landed files and manifests (the consumer's; Auto Loader can't watch the folder, see
  above), a Spark/PySpark reader (the DuckDB `changes()` and the `deliveries()` file lists are the reader for now;
  the MERGE recipe above is the Databricks path), an in-region throughput measurement from an App.

**Reading from Databricks, built 2026-11-09** (`duckstring.databricks`, `delta_log.py`; the author's calls: both
routes, since IT setups differ; the Volume route is expected to be the common one, and Databricks shouldn't have to
change how it writes). Two calls in Ripple code, each returning an `Input` (`version`, `changed`, `via`, `read()`,
`added()`, `NotAppendOnly`):
- `databricks.table(pond, "catalog.schema.table", warehouse=None)`: the version is Unity Catalog's table info
  property `delta.lastUpdateVersion` (measured: it advances on every commit, immediately; the GET takes about 50 ms
  and wakes no warehouse). Read from storage with `temporary-table-credentials` (`operation: READ`; Azure gives a
  user-delegation SAS, AWS temporary keys, mapped to a DuckDB secret scoped to the table's location) and
  `delta_scan(location, version => v)`; else, or for a view, a non-Delta table or one with
  `delta.feature.catalog*` (catalog-managed commits, which DuckDB's reader doesn't follow), through the warehouse
  (`warehouse=`, `DUCKSTRING_DATABRICKS_WAREHOUSE`, `DATABRICKS_WAREHOUSE_ID`) as `SELECT * ... VERSION AS OF v`,
  Arrow chunks saved as Parquet in the cache. With neither, the error names the refusal. `added()` on the warehouse
  is `v EXCEPT ALL previous` after a count of `previous EXCEPT ALL v` (non-zero = `NotAppendOnly`).
- `databricks.files(pond, path, format=None)`: a Delta table when `_delta_log/` exists, else Parquet. Any path
  `get_storage` takes; read in place when DuckDB can (a mounted Volume, local, an object store), else (a Volume
  through the Files API) mirrored into `DUCKSTRING_INPUT_CACHE` (default the temp dir; `duckstring-inputs/{hash of
  registry path and input}`), fetching only files it doesn't hold (names are immutable), 8 at a time, pruning
  what the current read doesn't need. Delta: `delta_log.files_at` replays the newest checkpoint (classic,
  multi-part or v2 with sidecars) and the commits after it to find the data files and deletion-vector files
  (`storageType u`: Z85-decoded uuid, verified against a Databricks table) a version reads, and mirrors those plus
  the log files. Parquet: Spark on Databricks writes data files named `...-tid-{id}-...`, `_started_{id}`, then
  `_committed_{id}` (JSON `{"added": [...], "removed": [...]}`) and `_SUCCESS` (measured from a serverless job on a
  Volume, plain and `partitionBy`); a file counts once a marker of its job is readable and no marker removed it;
  files without a tid count unless empty; `_`/`.` directories are skipped; `k=v` directories are Hive columns.
  The version is a digest of the committed files and sizes. `added()` = the new files; a missing previous file
  (an overwrite) is `NotAppendOnly`.
- Delta `added()` (both routes): when every commit since only adds files without deletion vectors, the new files
  are read through `delta_scan(..., filename = true)` filtered by name; otherwise (compaction, which Databricks ran
  on its own within a minute of a DELETE on the probe table, or an update) `v EXCEPT ALL previous`, after checking
  `previous EXCEPT ALL v` is empty. DuckDB's delta extension reads older versions (`version =>`) and deletion
  vectors (measured: the probe table's 90,000 live rows of 100,000).
- The position: `Pond._record_input` writes `(ref, f, state)` to the registry-only `_duckstring_inputs` at each
  read, keeping the previous run's row; `_input_state` takes the newest row at or before `previous_f` (so a Run
  dispatched while an earlier one is in flight, and a failed run, behave as Trickle windows do), else the published
  run stamp's `inputs` (`publish_plan` writes each input's newest state at or before the run's `f` there, over the
  previous stamp's), so a Duck that rebuilt its registry keeps it. `previous_f = NEVER` (first run, Rebuild): no
  position, everything is changed and added.
- Also fixed: `storage._azure_secret_body` built a SAS connection string without `AccountName`, which DuckDB
  accepts at `CREATE SECRET` and refuses at the first read ("invalid connection string", measured against ADLS), so
  a SAS-authenticated `abfss://` data root never worked. `databricks_rest.Client.save_result` is the Flock's result
  download, shared.
- Live, from the laptop on the hybrid workspace: through the Files API, the probe's Delta table (deletion vectors)
  1.6 s to open and 1.1 s to read cold, Parquet 1.2 + 0.4 s, partitioned Parquet 3.4 + 1.8 s (one listing per
  partition); unchanged next run 0 to 30 ms. Real Spark jobs between runs: an append gave 70 Delta rows (log only)
  and 50 Parquet rows; an append + `OPTIMIZE` + append gave exactly the 8 new rows (the difference path); a DELETE
  raised `NotAppendOnly`. A catalog table on the warehouse (vending refused): first read 2.4 s, unchanged 70 ms (no
  statement), an insert gave the 25 new rows, an update raised `NotAppendOnly`. The storage route is not yet run
  live: the test metastore has external data access off (`EXTERNAL_ACCESS_DISABLED_ON_METASTORE`), and the test
  service principal is neither metastore admin nor has MANAGE on `hybrid_dbx`, so the author enables it and grants
  `EXTERNAL USE SCHEMA`. Measured instead: DuckDB's `delta_scan` reads an ADLS Delta table with only a
  user-delegation SAS. AWS vending is written but unmeasured.
- Tests: `tests/test_databricks_inputs.py` (Spark-style output with markers, a hand-written Delta log, both through
  the fake workspace's Files API, the catalog on both routes, Rebuild, a lost registry, a failed run),
  `test_runtime.test_an_inlet_reads_only_what_databricks_added_since_its_last_run` (a real Duck).
- Not built: GCP vending (`gcp_oauth_token`; it falls back to the warehouse), Volume credential vending (the Files
  API is the route), CSV/JSON files, a column-mapped or partitioned Delta table through the fast `added()` path
  (column mapping and partition values are delta-kernel's, so they go through it; only the filename filter is ours,
  untested on partitioned tables), retention of the cache (it holds the current snapshot).

**Docs handoff** (to write in a fresh session; facts only). The "Running Duckstring on Databricks" guide, which
gathers the App hosting, reading, Spout, change-files and Flock material for Databricks users, has its own
handoff in `plans/databricks-guide.md`; the items below are the per-page reference and the other guides.
- Reference done here (change files): `formats.md` ("Change files", and `/Volumes/...` under `file://`),
  `cli/spout.md` (`--to`, `--mode`), matching the CLI help. Still to write: the Python reference page for
  `duckstring.landing` (`deliveries`, `changes`, `Changes`), and a `delivering_with_spouts.md` section on change
  files with the Databricks MERGE recipe.
- Reference done here: `environment.md` (`DUCKSTRING_VOLUMES`), `formats.md` (the `/Volumes/...` data root row).
- Reference done here: `environment.md` (the BigQuery and Databricks variables, the engine order, the credentials
  paragraph, the memory budget's container limit), `formats.md` (`databricks://`), `cli/duck.md` and
  `cli/spout.md` (engine and scheme lists, with the CLI help strings).
- Guides still to write: `offloading_large_recomputes.md` (BigQuery and Databricks sections, beside StarRocks and
  Athena, and the engine table at line 59; the "Data sent to MotherDuck leaves your AWS account" line is AWS-only),
  `cloud_compute_on_google_cloud.md` (the Flock on BigQuery: the API, the dataset with an expiration, the roles
  BigQuery Job User plus Data Editor on the dataset, the scratch prefix in the dataset's location),
  `cloud_compute_on_azure.md` (the Flock on Azure Databricks, the Entra path), `delivering_with_spouts.md` (a
  Databricks section: the destination, the permissions, the `_duckstring_f` column on append tables, warehouse
  time per delivery), and the Databricks App section of `hosting_on_a_platform.md` once the workspace run is done.
- Reading from Databricks (`duckstring.databricks`): a Python reference page (`table`, `files`, `Input`,
  `NotAppendOnly`, matching the module docstrings), `environment.md` (`DUCKSTRING_DATABRICKS_WAREHOUSE`,
  `DATABRICKS_WAREHOUSE_ID`, `DUCKSTRING_INPUT_CACHE`), and a guide (Getting Data In, or with the Databricks App
  hosting section): the two routes and when each applies, the metastore setting and `EXTERNAL USE SCHEMA` for the
  storage route, the warehouse fallback, writing to the Catchment's Volume (Parquet or Delta, any mode), `changed`
  + `pond.skip()` and `added()` + `append_table` vs `read()` + `merge_table`, checking `changed` before `added()`
  (an unchanged `added()` on the warehouse is still a statement), and the cache's disk use.
- The UI's Spout add form (`frontend/src/components/Sidebar.tsx`) offers `databricks://` (warehouse id, Volume,
  optional `client_id`/`client_secret` names) and `abfss://` since 2026-11-09, and `changes` as a mode.
