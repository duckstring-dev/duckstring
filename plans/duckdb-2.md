# DuckDB 2.0 support, a DuckLake Spout, and a look at Quack

Status: **Parts 1 and 2 built (2026-10-22)**, see "As built" at the end; branch `duckdb-2` off `main` at `ae1b6cc6`. DuckDB 2.0.0 LTS shipped
2026-10-21. This picks up after `plans/duckdb-2-ready.md` (items 1 to 3 built against the alpha) and answers
its items 5 and 6 (Quack) in Part 3. Three parts: the port (both packages), a DuckLake Spout, and the Quack
verdict.

Decided with the author (2026-10-22): keep the 1.5 floor (`>=1.5,<3`); Trickle releases as 0.2.1; the DuckLake
destination is DuckDB's attach string with Spout options as a trailing query; lake maintenance (compaction,
snapshot expiry) stays with the lake's owner, since Duckstring isn't the authority on someone else's lake.

## Measured on 2.0.0 (2026-10-22)

A throwaway venv with both packages editable and `duckdb==2.0.0`.

- **Trickle**: 136 failed, 49 passed, 1 skipped. Two causes only: 122 failures from open results, 13 from
  `DuckDBPyRelation.fetchone()` being removed.
- **Duckstring**: 169 failed, 1053 passed, 135 skipped (15m43s, most of it runtime e2e tests waiting out
  their timeouts). The same two causes: 72 open-result errors (plus the Flock's "could not read the job's
  SQL", which wraps one) and 65 relation `fetchone()`. Concentrated in `test_trickle` (36), `test_runtime`
  (21, downstream of the rest), the Flock suites (51), `test_change_detection` (16) and `test_data` (11).
  Nothing new beyond those two.
- **Probes** (2.0.0 unless noted):
  - The `COPY ... FILE_SIZE_BYTES 1` runaway (duckdb-2-ready item 2) is fixed in the final release: one file,
    returns at once.
  - Integer `//` and `%` by zero now raise (`Invalid Input Error: Division by zero`); `/` still gives
    `inf` (integer, DECIMAL, DOUBLE) and `0.0/0.0` gives `nan`.
  - Timezones work without ICU (`SET TimeZone`, `timezone('UTC', …)`, `date_trunc` on TIMESTAMPTZ).
  - `httpfs`, `postgres`, `sqlite`, `ducklake`, `quack`, `iceberg` and `icu` all install and load.
  - Parquet written by 2.0 (nested, DECIMAL, TIMESTAMPTZ) reads correctly on 1.5.6.
  - A DuckLake catalog created on 2.0 is version 1.1; DuckDB 1.5's extension refuses it ("catalog version is
    1.1, but the extension requires version 1.0") unless it migrates.
  - `pytz` is already declared by both packages; the earlier note about it was stale.
  - The deprecated Arrow fetch names are already avoided (`remote_duckdb` prefers `to_arrow_reader`).

## Part 1: the port

Trickle first (Duckstring depends on it), each green on both 1.5 and 2.0 before moving on.

### 1. Result lifecycle

In 2.0 `execute()` of a query starts it and rows are read as they're fetched; the result owns the
connection until it is exhausted, `complete()`d or `abort()`ed, and any other statement on that connection
raises `ResourceInUseException: connection has an unfinished result`. Writes (including `RETURNING`) still
complete inside `execute()`. The failure surfaces at the next statement, not at the culprit.

The culprit is almost always `con.execute(q).fetchone()`: one row read, result left open. Library sites:
39 `fetchone()` in Trickle and 93 in Duckstring (about 67 of them the `execute(...).fetchone()` form), plus
116 in tests. Also any query or `CALL` executed and never fetched.

- Add `trickle.db.first(con, sql, params=None)` (the first row or None) and `scalar(...)` (its first
  column), implemented with `fetchall()`, so they work on 1.5 and 2.0. Duckstring imports them from
  `trickle` (it already depends on it), keeping one definition.
- Replace every `execute(...).fetchone()` with them. Where a query can return many rows and only the first
  is wanted, add `LIMIT 1` rather than fetch everything.
- Errors after the first rows now raise from the fetch, not from `execute()`. Audit each `try:` around an
  `execute()` whose fetch sits outside it, and `core.retry_on_lock` callers that pass a lambda returning a
  result to fetch later; move the fetch inside.
- Cursors are already per thread (`executor` gives each Ripple a `.cursor()`; serving, pg wire and Flight
  take a cursor per query), so no cross-thread sharing to untangle.

### 2. Relations lost `fetchone()`

13 Trickle failures and 65 in Duckstring. `rel.fetchone()` becomes `rel.limit(1).fetchall()` (the error message's own advice),
through the same helper.

### 3. Integer division by zero in the Flock

DuckDB is the authority on results, and the StarRocks profile assumes DuckDB's `//` gives NULL on a zero
divisor, matching StarRocks' `DIV` (`flock/engines/starrocks.py`). On 2.0 DuckDB raises instead, so a
dispatched job would publish NULLs where a local run fails. `%` is the same.

- On a 2.0 Duck, rewrite `IntDiv` and `Mod` to abort on a zero divisor with `assert_true`, as `/` already does
  for 0/0. On 1.5, keep today's NULL. The Duck knows its version (`duckdb.__version__`), so the profile
  takes it as an input.
- Re-run the StarRocks conformance job; check Athena's measured list (it has no division today).
- Audit Duckstring's and Trickle's own generated SQL for `//` or `%` with a possibly zero divisor (the
  rank-Hilbert key, chunk sizing).

### 4. Bounds, CI and the image

- `duckdb>=1.5,<3` in both packages. 1.5 is an LTS until March 2027, so keep the floor and the
  `duckdb-floor` CI job.
- The main CI jobs then run on 2.0 (the latest); `duckdb-prerelease` follows 2.1-dev.
- Trickle releases as **0.2.1** (no API change, only the widened bound and the port), and Duckstring
  requires `duckstring-trickle>=0.2.1,<0.3`.
- The Duck image picks up 2.0 from the wheel's resolution; check its build.
- Keep `_MIN_CHUNK_BYTES` (harmless) and correct its comment: the runaway was an alpha bug.

### 5. Mixed versions

With `<3`, a Pond whose lock holds DuckDB 1.5 keeps 1.5 while the Catchment runs 2.0 (the Catchment's
Duckstring only replaces a DuckDB outside the range). That's useful: it's how a Pond keeps 1.x behaviour for
its own code during migration. What crosses versions:

- Parquet: fine both ways, except 2.0's shredded `VARIANT`, which a 1.x reader can't use (already a breaking
  type change in the contract).
- Registries: each is opened only by its own Duck. A downgraded Duck meeting a 2.0 registry already moves it
  aside and hydrates (duckdb-2-ready item 3).
- Change fingerprints are tagged `fp1-duckdbX.Y`, so the upgrade invalidates them: each opt-in table
  republishes once. Expected; goes in the release notes.
- The Flock runs in the Duck's own DuckDB, so item 3 keys on the Duck's version, not the Catchment's.

### 6. User code and docs

User Ripples run on the Pond's DuckDB, and the same `pond.con.execute(...).fetchone()` pattern breaks on
2.0. The release notes and the Python dependencies guide need to say so, with the fix and the per-Pond lock
as the escape hatch. Audit the demos and every docs code example (re-run the ones that change).

### 7. Not measured yet

Run each before release: the `dbt` extra (dbt-duckdb has made no 2.0 statement), Postgres egress, the
MinIO/object-store job, StarRocks conformance, the runtime e2e suite on both versions. MotherDuck can't be
tested here (no token, and no published 2.0 client support date); if `md:` refuses a 2.0 client, the Flock
falls back to local, which is the designed failure mode.

## Part 2: a DuckLake Spout

`plans/data-plane-choice.md` deferred external interop to "a Spout that writes to the user's own catalog",
naming DuckLake as the likely first target. This builds it. A Pond's output lands as DuckLake tables that
DuckDB, Spark, Trino and DataFusion can read, with DuckLake's snapshots, time travel and `table_changes()`
for the lake's own consumers.

### Tested on 2.0.0 (DuckLake 1.1)

- `ATTACH 'ducklake:meta.ducklake' AS lake (DATA_PATH 'files/')`, and the same with `ducklake:sqlite:…`.
- One transaction holding data writes to several tables plus a watermark row commits as one snapshot.
- `DELETE` then `INSERT` by key, `MERGE INTO`, `CREATE OR REPLACE TABLE`, `table_changes()`.
- `set_commit_message(author, message, extra_info => json)` inside the transaction.
- `ducklake_merge_adjacent_files`, `ducklake_expire_snapshots`, `ducklake_cleanup_old_files` run.
- **No PRIMARY KEY or UNIQUE constraints** ("not supported in DuckLake"): the Spout enforces keys itself, as
  the Postgres driver does.

### Design

- **Driver**: `egress/ducklake.py`, `SCHEMES = ("ducklake",)`. The Postgres driver is already plain DuckDB SQL
  over an `ATTACH`, so factor the shared part (attach once, watermark table, transactional apply, table
  creation from the relation's schema) into an attached-catalog base, and keep only the `ATTACH` in each.
- **Destination**: DuckDB's own attach string, with Spout options as a trailing query that the driver strips
  (the Postgres driver strips `schema` the same way). For example:
  - `ducklake:sqlite:/srv/lake/meta.sqlite?data_path=s3://bucket/lake/&schema=sales`
  - `ducklake:postgres:dbname=lake host=pg.internal password=${secret:LAKE_PG}?data_path=s3://bucket/lake/`
  - `ducklake:/srv/lake/meta.ducklake` (a DuckDB-file catalog: one process at a time, so only for a lake
    nobody else attaches while the Spout delivers)
- **Data path credentials**: `s3://`/`gs://` go through the object-store driver's secret creation and the
  data plane's endpoint helpers (`storage._s3_endpoint`), so MinIO and R2 work.
- **What each source becomes** (a new `Capabilities.requires_pk`, False here, True for Postgres, relaxes
  `_assert_transactional_pk` and the worker's check):

  | Source | Delivery |
  |---|---|
  | merge Trickle | changelog window: delete changed and removed keys, insert present rows |
  | append Trickle | the window's rows inserted (no key needed) |
  | overwrite Ripple | the table replaced in one transaction, only when the source version advanced past the watermark |

  Each delivery is one transaction: data, the watermark row in `_duckstring.egress` (inside the lake, so
  exactly-once as for Postgres), and a commit message (`author 'duckstring'`, message `{pond}@{major}`,
  `extra_info` with `f`). The overwrite replace uses `DELETE` plus `INSERT` rather than
  `CREATE OR REPLACE`, so the table keeps its identity and `table_changes()` stays continuous.
- **A `*` Spout commits every table in one snapshot.** The worker loops per table today; for a driver that
  can, wrap the loop in one transaction so lake readers see a whole run or none of it.
- **Schema evolution**: the version contract only lets additive changes through within a major, so a new
  column becomes `ALTER TABLE ADD COLUMN` and a widening becomes `ALTER COLUMN TYPE`. Postgres has the same
  gap today (`CREATE TABLE IF NOT EXISTS` never alters); fix both in the shared base.
- **Catalog version**: the Spout never migrates a catalog (no `AUTOMATIC_MIGRATION`), so it writes a 1.0 lake
  as 1.0. A lake it creates on DuckDB 2.0 is 1.1, which DuckDB 1.5 readers can't open; the docs say so.
- **Maintenance**: per-run commits make small files and many snapshots. Leave compaction and snapshot expiry
  to the lake's owner (they're retention decisions about someone else's lake) and document the three calls.
- **Test button**: `ATTACH` plus a schema listing, no writes, errors sanitised (the attach string may carry a
  password), as for Postgres.
- **Extension**: `ducklake` downloads on first use; an offline Catchment needs it installed beforehand.
  Works on 1.5 (DuckLake 1.0) and 2.0 (1.1).
- **Surface**: no new CLI or API shape (`spout add --to ducklake:…`). The UI's Spout form gains the scheme.
  Docs: the Getting Data Out guide and the egress reference.

### Tests

- Offline: SQLite and DuckDB-file catalogs with a local data path. Merge, append and overwrite sources;
  replay after a crash between apply and completion (exactly-once); a `*` Spout's single snapshot; an
  additive schema change; a real-Duck e2e on the Trickle demo.
- CI: a Postgres catalog (the `postgres-egress` service) with a MinIO data path (the `object-store` job).

## Part 3: Quack and serving

2.0 ships Quack as stable: `quack_serve()` (or `duckdb -serve`) serves a DuckDB instance over HTTP, and
clients `ATTACH 'quack:host' (TOKEN …)` or `CONNECT`. What decides its use here:

- Authentication and per-query authorization are hooks, but only SQL macros or compiled extensions; Python
  UDFs aren't visible to them.
- The server binds to localhost by default; anything beyond that should sit behind a TLS-terminating
  reverse proxy.
- Authorization sees the raw query text, so read-only enforcement means a read-only attachment or a
  separate instance.

Verdict by use:

- **Internal transport** (Duck to Catchment, the data plane, Draws): no. Ducks already dial back over HTTP,
  and data moves as Parquet files that any version reads. Quack would add servers without removing anything.
- **The catalog as a Quack front door** (`ATTACH 'quack:catchment'` from a user's own DuckDB): appealing, since
  the Catchment is the catalog and DuckDB users would get native types and full DuckDB SQL. But Taps can't
  work (a Tap query triggers a Refresh in Python, which the hook can't call), read and full keys need
  separate instances, and hosted platforms expose one HTTP port, so it needs a proxy story first. The key
  ladder itself could work (copy the hashes into the instance; a macro checks `sha256(token)`). The Postgres
  wire and Flight front doors already cover querying. Not in this branch; revisit if users ask.
- **A self-hosted Flock engine** (duckdb-2-ready item 6, `duckdb-server`): the one real fit. A big DuckDB box
  in the user's own account runs the builder's SQL unchanged, like MotherDuck, and most of the code is the
  existing remote-DuckDB engine. Open questions: uploading leaves over Quack, result throughput. Recommended
  as the next follow-up after this release, not in it.
- **`duckdb -serve` for local work** (Puddles): no need.

## Order

1. Trickle port, green on 1.5 and 2.0.
2. Duckstring port, including the runtime e2e suite on both.
3. The Flock division rewrite and the StarRocks conformance re-run.
4. The CI jobs not yet run on 2.0 (dbt, Postgres, MinIO).
5. The DuckLake Spout.
6. Docs and release notes.
7. Release, in the order already agreed: Trickle 0.2.1, then Duckstring 0.7.0 RC, the AWS smoke test, final.

## As built (2026-10-22)

**The port.** Both packages pass on 1.5 and 2.0.0: Trickle 186 + 1 skipped on 2.0 (187 on 1.5), Duckstring's
full suite on both, the dbt-mode e2e on 2.0 with dbt-duckdb 1.11.

- `trickle.io.first_row(result_or_relation)` replaced every library `fetchone()` (an AST rewrite, 248 sites in
  both packages and their tests). Demo Pond code got `.fetchall()[0]` instead, since it runs in a user's project
  (the first rewrite gave the demos a relative import of `trickle_io`, which failed every e2e that deploys one).
  Three docs examples were fixed the same way.
- **Found on the way: the NULL type.** 2.0 types a bare `NULL` (an all-NULL `VALUES` column, `SELECT NULL AS x`)
  as `"NULL"`. Stored, it refuses the next run's values; in a Parquet part it stops a glob reading the parts after
  it. `legacy_disable_null_type` restores 1.x, but it's a legacy flag and Trickle runs on users' connections, so
  instead nothing NULL-typed is stored: Trickle's four write entries (`append_table`, `merge_table`,
  `append_zset`, `apply_zset`) `concrete()` their input, plain tables publish through `_concrete_select`, and the
  contract reads the type the same way. All as INTEGER, which is what 1.x did.
- The StarRocks profile makes `//` and `%` by zero raise on a 2.0 Duck (`_ZERO_DIVISOR_RAISES`); DOUBLE `%` stays
  NaN on both versions.
- Bounds `duckdb>=1.5,<3`, `duckstring-trickle>=0.2.1,<0.3` (0.2.1 is the first 0.2 on PyPI; only 0.1.0 is out).
  `uv.lock` resolves 2.0.0 for Python 3.11 and up and 1.5.6 below: DuckDB 2.0 needs Python 3.11. No CI change was
  needed: the main jobs install the latest, `duckdb-floor` pins 1.5.0.
- Not run here: Postgres egress against a real Postgres, MinIO, StarRocks conformance (CI jobs), MotherDuck.

**The DuckLake Spout**, as designed, with these differences:

- The watermark stays beside the delivered tables (`_duckstring_egress` in the schema, as for Postgres) rather
  than in a `_duckstring` schema: keyed by table name, a shared table would collide across schemas.
- `AttachedCatalogDriver` (`egress/attached.py`) holds the shared half. Moving Postgres onto it fixed two things:
  a bootstrap reload created `_duckstring_f` in the destination and deltas left it NULL (system columns are now
  stripped), and a new source column is now `ADD COLUMN`ed.
- A replay writes nothing: the worker skips a table whose watermark covers `f`, so a lake gets no empty
  snapshot. An empty delta still advances the watermark (a deliberate Postgres rule: it keeps the cursor ahead
  of retention), so a run that changed nothing still commits one small snapshot.
- `spout resync` was a no-op for any destination with its own watermark (it only reset the engine's state). The
  next job now carries `resync`, and the worker reads from `NEVER`.
- UI: the Spout form has `ducklake:` with a data-path field (the s3/gs credential fields appear for a remote data
  path). Checked in a browser against a scratch Catchment running the demo: Test passed, Add created the Spout,
  and a Refresh of `reports` delivered `monthly_summary` through real Ducks as one `reports@1` snapshot.
- `test_a_lake_on_object_storage` puts the lake's files in S3 (moto locally) and its catalog in Postgres when
  `DUCKSTRING_TEST_PG` is set; CI's `postgres-egress` job now starts MinIO and runs it against both. Writes small
  enough for DuckLake's data inlining stay in the catalog, so the test includes a 5,000-row table.
