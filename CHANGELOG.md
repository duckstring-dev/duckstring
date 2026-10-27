# Changelog

Notable changes per release. Versions before 0.5.0 are recorded in the git history and the `v*` tags.

## Unreleased (0.7.1)

Faster incremental runs, a machine's memory shared out between its Ducks, and fixes for three ways an incremental
pipeline could publish a wrong answer. Needs `duckstring-trickle` 0.3.0.

### Fixes

- **Overlapping runs applied a change twice.** When a Pond Run started while an earlier one was still running (as
  a continuous Refresh often does), both read the same Source changes, and the Trickle builder and incremental
  aggregates applied them twice. Each Ripple now reads from its own last completed Run.
- **A run with nothing new to do could overtake one still running,** so the Ponds downstream never read that run's
  change. A run that would pass now waits for the run before it to finish. A failed run's retry also no longer
  passes over the change it failed on.
- **A Sink could lose rows while its Source compacted.** A Sink reading a Source as of an earlier run, while the
  Source folded or compacted its tables, missed the rows changed in between, and a Draw taken mid-publish could ship
  part of a run. Every read now names the files of one committed snapshot (the sidecar is written last, as the
  commit), and superseded files are removed only once no reader can still pick them.
- **Aggregate groups with a NULL key** fell out of date in incremental runs.
- **A Run dispatched to a Duck that was shutting down** after going idle waited for a Duck that never polled again,
  and failed as a dead Duck. A fresh Duck now takes it.

### Memory slots

Each Pond runs in its own DuckDB process, and each sized itself as if it had the machine (DuckDB's default is 80%
of RAM), so several running at once could overcommit memory until the kernel killed one. A machine's memory budget
(`DUCKSTRING_MEMORY_BUDGET`, by default 80% of RAM less 2 GB) is now divided into slots (`DUCKSTRING_RUN_SLOTS`,
default 4). A Duck gets its slots' share as DuckDB's memory limit when it starts work and drops to a small idle
limit when it has none (`DUCKSTRING_IDLE_MEMORY`, default 256 MB), and a Pond that's ready to run with no slot free
waits its turn, first come first served, shown as "Waiting for memory" in the web UI and `waiting` on
`/api/status`. A Pond running alone takes the spare slots too. A Python Ripple that holds data outside DuckDB
declares it, `@ripple(memory="6GB")` or `[pond] memory` in `pond.toml`, and takes the slots that cover it. A Run that
runs out of memory is retried once with every slot. On the TPC-DS benchmark this halved peak memory with no change
in run times.

Upgrading:

- With more than four Ponds running at once on one machine, some now wait for a slot. Set `DUCKSTRING_RUN_SLOTS`
  to the number that run together to avoid that, or to `0` to turn slots off.
- Migration `033` adds the declared memory to each Ripple.

### Faster incremental runs

On the TPC-DS benchmark at 1 TB, a change of new sales now takes 12.9 s (from 21.7), and a mixed change 37.5 s
(from 44.9). The main changes:

- A change that only adds rows (an append Trickle, or a merge window that only inserted new keys) is written without
  consolidating it, and a join whose other side didn't change reads that side once. `Delta.insert_only` reports it.
- A builder run reads only the current states its terms use.
- An incremental min or max rescans a group only when a removed row may have held its extreme.
- An aggregate's published state is written as its change each run, rather than in full.
- A merge table's first load goes straight to its base, and a large append run is published as chunks written in
  parallel.
- A Ripple's windowed Source change is read once per run, and the change-fraction check counts a Source's rows from
  metadata.
- A Duck collects a new job as soon as it's queued, rather than at its next poll, so each pipeline stage starts at
  once.

### Trickle 0.3.0

`Context.apply_zset` takes `insert_only`, which the builder passes for a change that only adds rows, so a host
must accept it. Duckstring 0.7.1 requires Trickle 0.3; 0.7.0 stays on Trickle 0.2. `Delta.insert_only` is new.

## 0.7.0 — 2026-10-22

A single trigger, Refresh, replaces Tap, Wave, Pulse and Tide, and a Tap is now an app's access point to a
Pond. Also DuckDB 2.0 support and Spouts that deliver to a DuckLake. The old trigger commands and routes are
removed with no compatibility period, so read each section's Upgrading notes. Needs `duckstring-trickle` 0.2.1.

### One trigger: Refresh

Tap, Wave, Pulse and Tide are replaced by a single trigger, **Refresh**, which asks for a Pond's data to be at
least as fresh as now. A recurring Refresh repeats it:

- `duckstring refresh add reports --every 1d`: a day after the later of the last request and the last refresh of
  the data (the old Tide).
- `duckstring refresh add reports --every 1d --start 06:00 [--on MON,TUE,WED,THU,FRI] [--until T]`: at set times,
  on a window's grid, whatever else refreshes the Pond. New. A Catchment that was down asks once, for the latest
  occurrence, when it returns.
- `duckstring refresh add reports --continuous` (or `--every 0s`): as fresh as the pipeline allows, paced to its
  slowest step (the old Wave).

`duckstring refresh now reports` refreshes once (the old Pulse). A Pond can have any number of scheduled Refreshes and
one `--every` or continuous Refresh; `refresh ls` lists them with when each next asks, and `refresh rm` removes
one. In the web UI, a Pond's trigger buttons became a Refresh section (Refresh Now, and Recurring Refresh for
Continuous, Every N and schedules), and its recurring Refreshes and Taps are listed in a strip at the bottom of its
box on the canvas.

Upgrading:

- **Existing Waves become continuous Refreshes and Tides become `--every` Refreshes** (migration `031`).
- **`POST /api/ponds/{name}/refresh` changed meaning.** It used to mark a Pond for a rebuild; it now refreshes the
  Pond (at `demand` level). The rebuild flag moved to `POST /api/ponds/{name}/rebuild`, and the command to
  `duckstring control rebuild` (and the batch operation `refresh` to `rebuild`).
  Scripts that called `/refresh` for a rebuild need updating.
- **Pull is gone from the engine.** A Tap's "anything newer" request no longer exists: a Refresh always asks for
  data as of now. Wake now runs once if the Sources are fresher at that moment, rather than waiting for them.
- `duckstring trigger window POND add|list|remove` is now `duckstring window add|ls|rm POND`.
- **The old commands and routes are removed, with no compatibility period**: `duckstring trigger …`, `control
  refresh`, `do --refresh`, and `POST /tap`, `/pulse`, `/wave`, `/tide` and `/untrigger`. Scripts need the new ones.
- **Both ends of a duct need this release.** A Draw sends no requests to an upstream Catchment older than Refresh
  (there, `/refresh` would mark the Pond for a rebuild) and logs that it needs upgrading.
- **`catchment open --tap-on-get` is removed** (it went with Tap, and fired on every reader, including the web
  UI's data viewer). An open Pond stays open. Taps, below, replace it.
- `/api/status` lists each Pond's `refreshes` (with `next_due`), `demand_f` and `rebuild_pending`, in place of
  `trigger`, `has_pull`, `target_f` and `refresh_pending`.
- The Orchestration Theory page was removed from the docs; the Scheduling guide and the `refresh` and `window`
  CLI reference cover what it said that users need.

### Taps: freshness that follows usage

A **Tap** is an access point on a Pond for one app: `duckstring refresh tap add reports --name dashboard --fresh-within
5m`. It has its own key, shown once, which reads the Pond's exposed tables (as a read-level key does) over HTTP
(`/api/query`, `/api/serve/query`), the Postgres wire or Arrow Flight, and is refused everywhere else. A query through it sends the Pond a Refresh when the newest request on the Pond, or the
data, is at least `--fresh-within` old (the `--every` clock, checked per query; `0`, the default, refreshes on every
query unless a request is already waiting), so the pipeline keeps up while the app is in use and goes quiet when it
isn't. A query is always answered at once from what is published. Queries with any other key never send demand.

- `refresh tap ls|rm|rotate POND`; the routes `/api/ponds/{name}/taps` (full). The web UI lists a Pond's Taps at the
  foot of its Refresh section, adds them in a modal, and shows a Tap's details, Rotate and Delete when clicked.
- `/metrics` gains `duckstring_tap_queries_total` and `duckstring_tap_refreshes_total`; `/api/status` lists each
  Pond's `taps` (never keys).
- **`GET /api/ponds/{name}/freshness`** says how fresh a Pond's data is: `freshness` (the point in time it
  reflects), `changed` (when its output last changed), `refreshing`, and when each table and Object last changed. A
  Tap's key can call it for its own Pond, and `Catchment.freshness()` wraps it. It replaces a route of the same name
  that the HTTP reference described as the Pond's freshness but that listed a Trickle table's run freshnesses for
  the data viewer; that list moved to `/api/ponds/{name}/tables/{table}/runs`.
- The Python `Catchment` client takes a Tap's key as `api_key`, and then needs no `pond`.
- The demo `reports` exposes `monthly_summary` (`[serve] tables`), so a Tap on it works out of the box.
- Migration `032` (`pond_tap`).

### The CLI, regrouped

The top level goes from 21 commands to 13: `status`, `query`, `get`, `refresh`, `window`, `control`, `data`, `pond`,
`catchment`, `duck`, `spout`, `secret` and `alert`. Tables and Objects are named `POND.NAME` everywhere, as in the
Python API. No old forms are kept:

| Before | Now |
|---|---|
| `query POND [TABLE] [--sql SQL]` | `query POND.TABLE`, or `query SQL [--pond POND]` |
| `serve query SQL` | `query SQL` (with `--csv`, `--json` and `--parquet` too) |
| `get POND TABLE [--path]`, `get-object POND NAME [--out]` | `get POND.NAME [--out]` |
| `objects POND`, `serve status` | `data ls [POND]` (with each table's exposure and last change) |
| `delete-table POND TABLE`, `delete-object POND NAME` | `data delete POND.NAME` |
| `serve expose POND TABLE` | `data expose POND.TABLE` |
| `lineage [POND] [--table T]`, `trace POND.TABLE` | `data lineage [POND[.TABLE]]`, `data trace POND.TABLE` |
| `serve promote POND` | `pond promote POND` |
| `puddle ls\|show\|query` | `pond puddle ls\|show\|query` |
| `catchment open\|close` | `catchment duct open\|close` |
| `do PONDS --kill …` | `control kill PONDS …`: every `control` verb takes several Ponds and `--all`, `--tree`, `--between`, `--downstream`; `--wipe` is `control wipe`, `--remove` is `pond remove` |

`POST /api/serve/query` takes a `format` (`csv`, `json` or `parquet`) to return a file.

### Orchestration

- **Requests that arrive faster than the pipeline can serve them are paced to its slowest step** (the throttle),
  always on, with no setting. A step that receives two requests during one run is held there, and one request goes
  upstream each time it starts a new run.
- **A Refresh no longer refreshes an optional Source after its first build.** Demand propagates only to a Pond's
  required Sources: a Pond never waits for an optional Source, so asking it ran it for nothing. An optional Source
  that has never run still gets one request, so its Sink has output to read, and a Pond whose Sources are all
  optional still asks every one of them. If an optional Source needs a cadence of its own, give it its own Refresh.
- **A repeat request for the freshness a Pond is already running is ignored.** It used to start a second
  Run at the same freshness, which the no-change skip then completed as a pass, ending the Run in flight
  early. A duct re-sending its downstream's request could cause it.
- **A join's Sources read together under saturation.** When some of a Pond's Sources are throttled and others
  aren't, its requests reach the faster Sources only once the throttled ones start their next run, so a join
  isn't held back by a fast Source that read early.
- **A Pond already throttled through another Refresh's Source still sends its first request.** It used to hold
  that request until its own first completion, which the request itself was needed for.
- **Sleep keeps other Ponds' demand.** Sleeping a Pond (or a path with `--upstream`) clears its own demand, but a
  pending request from another Pond it was serving is now sent to it again; before, that request stranded.
- **Demand is no longer lost at a halted Source.** A Pond that stops being blocked (after Clear, a recovery or a
  redeploy) re-sends its newest unmet request upstream. Before, a request waiting on a Source whose Run had failed
  or been killed stranded after Clear until something else demanded the path.

### Redeploys and contract failures

- **A redeploy takes effect while a recurring Refresh keeps the Pond busy.** A warm Duck used to keep running the
  code it started with until it went idle, which a continuous Refresh never let it do, so a fix redeploy changed
  nothing. A Duck now says which version it runs when it asks for work; a Duck on a replaced version finishes the
  Runs it holds and exits, and a Duck on the new version picks up the next Run.
- **A Run in flight across a redeploy is recorded against the version that started it.** Its events were recorded
  against the new version, which the database refused, and the Duck retried the refused event forever: the Pond
  showed as running for good, and nothing it did afterwards was recorded.
- **A contract violation fails the Pond.** The Duck reported the Run's last Ripple as a success before it found the
  output broke the version's contract, so the Catchment counted the Run done and dropped the failure that followed:
  the Pond never showed as failed, and its Sinks ran on output that was never published. A Duck from before this
  release still reports the old way, so redeploy Ducks with the Catchment.

### DuckDB 2.0

Duckstring and Trickle support DuckDB 2.0 as well as 1.5 (`duckdb>=1.5,<3`). DuckDB 2.0 needs Python 3.11 or
newer, so on Python 3.10 DuckDB stays at 1.5.

Upgrading:

- **Ripple code may need a change.** In DuckDB 2.0 a query run with `execute()` holds its connection until its rows
  are read to the end, so `pond.con.execute(...).fetchone()` followed by another statement fails, and relations
  no longer have `fetchone()`. Read with `fetchall()`. A Pond whose lock keeps DuckDB 1.5 stays on 1.5 while the
  Catchment runs 2.0, so Ponds can move over one at a time.
- A column that is NULL in every row and has no other type (`SELECT NULL AS note`) is published as `INTEGER`, as
  DuckDB 1.5 typed it. DuckDB 2.0 would give it its own NULL type, which refuses later values.
- Tables written with `if_changed=True` republish once after the upgrade, since change fingerprints are tied to
  the DuckDB version.
- A Pond database written by DuckDB 2.0 can't be opened by 1.5. A Pond moved back to 1.5 rebuilds its database
  from what it published, as it would after losing it.
- On DuckDB 2.0 an integer `//` or `%` by zero raises. A Flock job on StarRocks now fails in the same place and
  runs locally, rather than publishing NULL.
- Trickle 0.2.1 is required.

### DuckLake Spouts

A Spout can deliver to a [DuckLake](https://ducklake.select):
`duckstring spout add reports --to 'ducklake:sqlite:/srv/lake/meta.sqlite?data_path=s3://lakehouse/files/'`. The
destination is DuckDB's own attach string for the lake (SQLite, Postgres or DuckDB catalogs), with `data_path`,
`schema` and the data path's credentials as options. Any table can be delivered: a merge Trickle by its primary
key, an append Trickle's new rows, a plain table replaced whole. A run's tables go in one transaction, so the
lake gains one snapshot per delivery, authored `duckstring`. Delivery is exactly-once. Duckstring never
migrates a lake's catalog, and leaves compaction and snapshot expiry to the lake's owner.

Also for Spouts:

- `spout resync` now re-delivers in full to Postgres too. Before, a destination that keeps its own record of
  what it has received (Postgres) treated a resync as already delivered.
- A Postgres table first filled by a full load no longer gains a `_duckstring_f` column that later deliveries
  left empty, and a column a table gains is added to the destination table.
- A Spout delivering all of a Pond's tables no longer delivers a merge Trickle's changelog as a table of its own.

### Contract checks on remote Ducks

A merge or append Trickle with an integer `SUM` column failed its version contract on the first run of a fresh
Duck (every run on Fargate, EC2 or a pool, after its idle shutdown). `SUM` of an integer is `HUGEINT`, which
Parquet stores as `DOUBLE`, so a Duck that rebuilt its database from what it had published held `DOUBLE` where the
contract had recorded `HUGEINT`. The check now compares types as published. Before this release, contract
failures found at that point were dropped without a trace, which hid it.

### Documentation and Playground

The Concepts and Guides have screenshots and recordings of the web UI and the Playground, and the landing page
leads with the Playground. The Playground has a `chain` example (`?example=chain`), the 1 s → 3 s → 1 s chain the
Orchestration page uses.

## 0.6.1 — 2026-10-08

### Performance

- **Clustering uses a rank-Hilbert order.** Interleaved `cluster_by` columns are now mapped onto a
  Hilbert curve instead of a Morton (Z-order) curve, which on TPC-DS reads 20 to 40% fewer rows for a
  filter on one clustered column at the same cost. Each column's rank is found by binary search over
  quantile boundaries computed in one pass (approximate on tables over ten million rows), replacing
  window functions that sorted every row once per column: on 288 million rows the old encoding filled
  60 GiB of spill and failed, and the new one adds a few seconds to the sort. Equal values now always
  share a bucket, so the order is deterministic for tables under ten million rows (above that, the
  approximate boundaries can shift slightly between rewrites). `cluster_bits` is rounded up to a
  multiple of the number of columns. A table already clustered is reordered at its next compaction.

## 0.6.0 — 2026-10-05

SQL Ripples, clustering, Pond environments and a simpler data plane. The Iceberg plane is gone: every
Catchment now publishes versioned Parquet, which pins each run's Source reads and costs a third of the
export time. See "Upgrading" at the end.

### Building Ponds

- **SQL Ripples**: a Ripple can be one `SELECT` in a file, declared in `pond.toml` under
  `[ripples.NAME]` with its `parents`, the Source tables it `reads`, and how it writes (`overwrite`,
  `merge` or `append`). Nothing is inferred from the SQL; each query is checked against its declaration
  at deploy, so a forgotten parent is refused instead of silently reading the previous run's table. SQL
  and Python Ripples mix freely (`@ripple(parents=...)` now takes names), and column lineage is
  recorded for SQL Ripples too. `duckstring pond demo --sql` shows the default pipeline in SQL.
- **Static tables**: files shipped with a Pond's code, declared under `[static.NAME]`, are read-only
  tables every Ripple can query.
- **Pond environments**: a Pond with a `pyproject.toml` and `uv.lock` runs in its own environment, built
  at deploy on the Catchment and by cloud and Pool Ducks on their own machines. A missing or stale lock,
  or code that won't import, now refuses the deploy with the reason (an import error used to register no
  Ripples).
- **`.pondignore`**: chooses which files a deploy uploads; by default `puddles/`, `.env` files, hidden
  directories and caches stay behind.
- Names containing dots can be referenced with backticks (``sales.`daily.v2` ``).
- A merge Trickle's name is a view over its current state, so plain SQL in later Ripples reads what
  `read_table` returns.

### Performance

- **Clustering**: `cluster_by` on `merge_table`, the builder's `.merge()`, `write_table` and SQL Ripples
  orders a table for reads that filter on those columns. One column sorts; two or more are interleaved
  with a rank-Morton order (each column's quantile ranks, bit-interleaved), so every listed column
  prunes, skewed ones included. A merge table's compacted base is now ordered by its primary key by
  default instead of by freshness, which no read could use.
- A merge table's warm tier is read where it's published instead of being copied into a fresh Duck's
  database, and is no longer also re-published in full on every export.

### Data plane

- **Versioned overwrite tables**: each run publishes a plain table as a new immutable version, and a
  Sink run reads the version its Source had published when the run started, even if the Source publishes
  again meanwhile. Superseded versions are kept only while a running Sink still reads them.
- **The Iceberg plane is removed**, with `pyiceberg`, `DUCKSTRING_DATA_PLANE` and
  `DUCKSTRING_ICEBERG_KEEP_SNAPSHOTS`. It cost about three times the export time, more than twice the
  memory and over three times the disk of the Parquet plane, for interop no external engine could use.
- A registry DuckDB can't open (written by a newer DuckDB, or corrupt) is set aside and rebuilt from
  published state instead of stopping the Duck.

### Cloud and egress

- Flock settings and engine credentials travel with each run, so Fargate and EC2 Ducks can use the
  Flock; they received neither before.
- The **Duck image is published** to GHCR for amd64 and arm64, tagged with the version, so cloud Ducks
  need no image of your own unless Ponds need system libraries.
- The S3 Spout now honours `?endpoint=` and `DUCKSTRING_S3_ENDPOINT`, so it works with MinIO, R2 and
  Ceph; it used to reach only AWS. Tested against MinIO in CI.
- A failed Spout delivery is retried when its source next publishes, and a Spout or alert destination
  can be a single `${env:...}` or `${secret:...}` reference.
- A Pond Draw records its upstream's real version, so a `[sources]` pin above `{major}.0.0` deploys.
- A Duck that lost its local publish restores it from the durable layer before rebuilding, so Ponds on
  the same machine never read it incomplete; the durable copy no longer loses warm bands after such a
  rebuild.

### Correctness and operability

- **DuckDB is bounded** to the tested range, `>=1.5,<2`, and CI tests the floor and the DuckDB 2.0
  pre-release. A change to or from DuckDB 2.0's `VARIANT` type counts as breaking in the version
  contract.
- `catchment init` and `start` warn when an open Catchment binds beyond loopback, since a deploy runs code
  on the machine.
- Querying a missing table says the Pond hasn't run yet, or lists the tables it has.
- A Pool agent stopped with SIGTERM stops its Ducks.
- A Fargate or EC2 Duck shut down for being idle is replaced on the Pond's next run. Before, the Catchment
  kept the departed Duck's record, launched nothing, and failed the run as "Lost contact" three minutes
  later.
- An EC2 Duck's instance terminates when the Duck exits cleanly, instead of running, and billing, after
  the Duck has gone. A Duck that crashes leaves its instance up so its console output can be read.
- SQL Ripples that read a Source work on the DuckDB 2.0 pre-release.
- A Catchment without API keys could answer a request with 401 under load. The Catchment's database
  connection is shared by its threads, and the key check read it without the lock the engine holds,
  so it sometimes saw a key that wasn't there. Every user of the connection now takes that lock, and
  the key check is cached.
- A Duck that has heard nothing from its Catchment for an hour while idle exits, so a Duck left behind by
  a Catchment that was deleted or stopped hard doesn't run, and bill, indefinitely. Set
  `DUCKSTRING_DUCK_ORPHAN_MINUTES` on the Catchment to change the limit, or `0` to turn it off. Pool
  machines aren't affected.

### Documentation

The documentation is rewritten: Quickstart, Concepts, task-led Guides and a full Reference (Python API,
`pond.toml`, every CLI command group, the HTTP API, formats and environment variables), with the README and
landing page repositioned around Duckstring as a data engineering platform for DuckDB.

### Upgrading

- **Published layout**: plain tables are now written as `{table}__v/{version}.parquet`. Readers still read
  a Source's old single `{table}.parquet` until it publishes again, so Ponds can be upgraded in any order.
- **Leftover Iceberg files**: a data directory published by the Iceberg plane keeps a `catalog.json` and a
  `pond/` directory that nothing reads now. Delete them; on a Catchment using local-first publish with an
  S3 data root, the Persist mirror would otherwise upload them.
- **`DUCKSTRING_DATA_PLANE`** is no longer read; remove it from the environment.
- **DuckDB 2.0** isn't supported yet: installing Duckstring keeps DuckDB below 2, including in Pond
  environments that lock a newer one.
- The Catchment database migrates itself on start (one new column).

## 0.5.0 — 2026-08-03

The cloud release: a Catchment can now run its Ducks on AWS, keep its data on S3, and serve that data
over standard wire protocols. Everything below is opt-in — a Catchment with no cloud configuration
behaves exactly as it did in 0.4.0, and a `pond.toml` asking for remote compute still runs anywhere.

### Cloud compute

- **Duck launchers**: a Pond's Duck runs on the Catchment's own box (default), on **Fargate** (the
  default remote backend — serverless containers, task-role IAM), or on **EC2** (the escape hatch, for
  sizes and GPUs Fargate does not offer). Built-in **S/M/L/XL preset pools** resolve with zero setup.
- **Duck Pools**: named remote-compute pools (`duckstring duck pool add`), with per-Pond compute
  declared in `pond.toml` and overridable operationally (`duckstring duck set`).
- **The Pool agent**: one shared machine per named Pool hosting many co-resident Ducks.
- **Auto-relay**: a Catchment on a laptop behind NAT can run cloud Ducks with no manual tunnel — the
  first remote spawn provisions a small always-reachable box and holds a reverse tunnel to it.
- **Remote failures are observable**: boot output is teed to the console (EC2) / CloudWatch (Fargate)
  and the tail is attached to the Pond's failure, so a Duck that dies before dialling back still says
  why. Spawn-failure reasons (missing image, missing AMI, bad IAM) surface on the Pond rather than as a
  generic crash.
- Per-provider **startup grace** so a cold machine is not judged by the steady-state silence window.

### Data plane on S3

- A Catchment's data root can be an object store (`duckstring catchment settings --data-root s3://…`),
  with an S3-compatible **endpoint override** for MinIO/Ceph/R2.
- **Switch, adopt, or migrate** an existing plane, with a background copy and live progress.
- **Local-first publish + async Persist**: a run publishes locally and mirrors durably in the
  background, so compute never waits on the object store; `persisted_f` is the durable watermark and
  freshness gating is Pool-aware.
- **S3-resident state**: a merge base is read as a view rather than hydrated, and retention pruning is
  floor-anchored rather than inferred from absence.

### The Flock — over-envelope compute

A comprehensive `pond.trickle(...)` recompute that exceeds the Duck's memory envelope can be dispatched
to a serverless engine (**Athena** ships first), while the Duck keeps merge/diff/publish.

**DuckDB is the authority.** Dispatch decides *where* work runs, never *what* is published, enforced
structurally: an engine-owned allow-list of expressions proven equivalent on both engines (division and
CAST are excluded — the two engines genuinely disagree), a `conform` step that casts the engine's result
to DuckDB's own schema and rejects a differing column set, and degradation to local compute on any
failure. Dispatch counters ride `/metrics`, since a silently degrading Flock is otherwise invisible.

### Data serving

- A sandboxed, warm, read-only query surface over published data: catalog = catchment, schema = pond.
- **Postgres wire** and **Arrow Flight SQL** adapters, plus `/api/serve` and a CLI.
- A **Catalog UI** unifying the query surface, with role-governed access.

### Lineage

- Table-level (observed per run), column-level (static per version, with a sqlglot upgrade for SQL
  outputs), and row-level temporal provenance (`duckstring trace`).
- **OpenLineage** events emitted on run completion for catalog integration.

### dbt-mode Ponds

Deploy a dbt project as a Pond with no `@ripple` code: each dbt model becomes a Ripple with its own
freshness, failure and retry tracking, and `ref()` becomes the intra-Pond graph. Opt in with the
`duckstring[dbt]` extra.

### Correctness and operability

- **Version contract**: a lossless widening of a column type is accepted (a data-dependent branch could
  legitimately wedge a live Pond permanently); `reset-contract` is the escape hatch for a genuine
  narrowing; contract failures carry a dedicated sub-reason end-to-end.
- **Reads reject a stale local publish** the Catchment knows is behind, rather than serving it.
- Pond Runs the Pond has moved past are closed rather than stranded at `running`.
- A Pool machine that disappears is detected and relaunched, instead of wedging every Pond on it.
- The **serving sandbox** no longer spills into the shared system temp — DuckDB exempts its temp
  directory from the external-access lock, which on Linux exposed everything under `/tmp` to a
  read-level user.
- An idle Duck backs off instead of polling ten times a second.
- Opt-in **re-notify cadence** for alert channels while a failure or freshness episode persists.
- Incremental object-store egress (`mode=append`) mirrors the published collection per delivery.

### Testing

- The object-store data plane runs against a **real S3 API** in CI (MinIO, which rejects unsigned
  requests — the class of bug moto cannot catch).
- Real-data demo sets: **TPC-DS** and **GHArchive** (the latter offline against a committed fixture).
- Python matrix, the dbt extra, real-Postgres egress, and frontend tests in CI.

### Documentation

- A full **AWS guide** (`guides/cloud.md`): the cloud-enable gate, the three IAM roles, security-group
  asymmetry, the AMI Python constraint, the build-your-own-image policy, and the Flock's authority rules.
- New guides for lineage, dbt-mode, and querying data.
