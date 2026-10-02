# DuckDB 2.0 ready

Status: **planned (2026-10-03)**; one fix already in (`1efd8af9`). DuckDB 2.0 is due in the second half of
October 2026. Goal: Duckstring works on it on release day, and uses the 2.0 features that matter to it.

## Why now

Duckstring depends on `duckdb>=0.10` with no upper bound, so on 2.0's release every fresh
`pip install duckstring`, and every Pond environment that doesn't lock DuckDB, gets 2.0. The floor has
never been tested either: CI only installs the latest DuckDB.

## Compatibility, as tested on the alpha

Tested 2026-10-03 against `duckdb 2.0.0.dev2610011535` (the full unit suite, file by file, and the
runtime end-to-end suite):

- **Arrow lambdas are rejected by default.** The merge-table view used `COLUMNS(c -> ...)`. Fixed in
  `1efd8af9` (`lambda c: ...`, which works on 1.5 and 2.0). It was the only instance in `src/` and
  `tests/`.
- **A DuckDB bug: `COPY ... TO <dir> (FORMAT PARQUET, FILE_SIZE_BYTES n)` hangs once it needs a second
  file.** Duckstring writes a merge table's cold base this way (`dataplane._publish_base_chunks`), so on
  2.0 the first cold compaction of a merge table whose base exceeds the chunk size (256 MiB by default)
  hangs that Pond's publish. Standalone reproduction (finishes instantly on 1.5.5):

  ```python
  import duckdb
  con = duckdb.connect()
  con.execute("CREATE TABLE t AS SELECT i AS id FROM range(1000) r(i)")
  con.execute("COPY t TO 'out' (FORMAT PARQUET, FILE_SIZE_BYTES 1)")  # hangs
  ```

  The author to report it upstream. The four test files that cover chunked bases (`test_egress_file`,
  `test_iceberg`, `test_merge_view`, `test_trickle`) hang on it; their other tests pass.
- **Everything else passes**: the other 65 test files, and the runtime suite with real Ducks (25 tests).
- Not yet tested: the `dbt` extra (dbt-duckdb on 2.0), the Postgres egress extension and MinIO, which
  are CI jobs.

## Work

### 1. Version bounds and CI

- Floor: the oldest DuckDB actually tested (1.5), replacing `>=0.10`.
- Ceiling: `<2` until item 2 is resolved and the suite passes on the 2.0 release, then `<3`.
- A CI job running the suite against the DuckDB pre-release (`pip install --pre duckdb`), allowed to
  fail, so the next breaking change shows up before a release instead of after.
- Pond environments: `duckstring_requirement` installs the Catchment's own Duckstring, whose bounds then
  constrain the environment's DuckDB, so a Pond can't drift outside the tested range.

### 2. The chunked-base hang

If DuckDB fixes it before 2.0 ships, nothing to do but re-test. If not, stop relying on
`FILE_SIZE_BYTES` rollover: estimate rows per chunk from the table's size and row count, and write each
chunk with its own `COPY` of a `_duckstring_f`-ordered row range (the chunks only need to be
freshness-ordered and roughly size-bounded). Keep the token-and-swap commit as it is.

### 3. An unreadable registry is rebuilt like a missing one

2.0 has a new default storage format, and with Pond environments different Ponds can run different
DuckDB versions. A Pond's `registry.duckdb` written by 2.0 and later opened by 1.x (say, the Pond locks
an older DuckDB) fails to open, and the Duck dies at start-up: registry-loss recovery
(`duck/executor.py`) is keyed on the file being missing, not unreadable. Treat a storage-version or
corruption error at open the same way: move the file aside (keep it, for inspection), then hydrate from
published state. Published data crosses Ponds as Parquet, which stays readable across versions (except
2.0's shredded `VARIANT`, which a 1.x reader can't use).

### 4. Asynchronous I/O: automatic, mostly

2.0's async I/O (DuckDB measured 2-3x faster S3 Parquet reads) applies to everything DuckDB reads and
writes itself: Source reads, exports, registry hydration, the serving core's materialisation, the Flock's
staging, and Spout writes to S3. Nothing to change there.

It doesn't apply to the paths that move bytes through fsspec (`storage.ObjectStorage`): the Persist
mirror's uploads (`dataplane.persist_tree`), Draw transfers, sidecars and listings. Persist is off the
critical path and Draws are cross-Catchment, so this is low priority; if the Persist mirror ever matters,
upload its files concurrently or route Parquet through DuckDB.

### 5. Serving the catalog over DuckDB's own protocol

2.0's client/server mode (the `quack` extension and `CONNECT`) lets a user attach a remote DuckDB from
their own DuckDB: `ATTACH 'quack:catchment.internal:PORT' AS ds (TOKEN '...')`. Offer that alongside the
Postgres wire and Flight front doors (`catchment/pg_wire.py`, `flight_sql.py`), so the catalog is
queryable from any DuckDB with full SQL and DuckDB's own wire format.

What's needed, unlike the existing front doors:

- **A long-lived instance per access level.** The Postgres and Flight front doors call the serving core
  once per query with the caller's key. A quack server serves one DuckDB instance, so the Catchment runs
  one sandboxed instance for read keys (the materialised serviceable tables, with
  `enable_external_access = false` and `lock_configuration = true`, as `serving.py` builds today) and one
  for full keys (views over every output table), each refreshed when `Driver.data_version` changes.
  This is also the "warm, resident" lifecycle `serving.py` lists as a follow-up.
- **Auth mapped to the key ladder.** Each instance accepts only keys of its level or higher (the ladder's
  keys are stored as hashes, so this needs quack to verify through a hook, or one token per level).
  Platform auth deployments rely on the platform's gate instead, as for the other front doors.
- **Config** like the others: `DUCKSTRING_SERVE_QUACK_PORT`, bound with `DUCKSTRING_SERVE_HOST`; docs in
  the Querying guide and `environment.md`.

### 6. A self-hosted Flock engine

The same client/server mode gives a self-hosted counterpart to MotherDuck: a DuckDB server on a large
machine in the user's own account. It has the property that makes MotherDuck a good Flock engine (the
builder's own SQL runs unchanged, so results match by construction) without data leaving the account,
and with Duckstring choosing the DuckDB version at both ends.

- **One engine, two targets.** Generalise `plans/flock-motherduck.md`'s engine into a remote-DuckDB
  engine: `motherduck` connects to `md:`, `duckdb-server` (working name) connects to a `quack:` address.
  Leaf substitution, as-of pinning, remote execution, result transfer, `conform` and the fallback are
  shared; only connection and credentials differ.
- **Running the server.** `duckstring flock serve [--port] [--memory-limit]`: starts DuckDB with the
  quack server, `httpfs`, an S3 secret from the machine's credential chain (an instance role, never a key
  on disk), and a token from the secret store. Documented for an EC2 instance in the Ducks' VPC, reachable
  from `sg-duck` only.
- **Later: start it on demand.** The EC2 launcher already starts machines for Pools; the Flock server
  could start on the first dispatch and stop after an idle period. Not in the first version.

## Lower priority

- Measure 2.0's wider row-group pruning (`IN` filters and function predicates) against the Trickle
  builder's key-filtered joins over Parquet; it may cut what an incremental join reads.
- Partition-aware queries: little value while Duckstring's Parquet layout isn't Hive-partitioned.
- Recognise `VARIANT` in the schema contract's type handling, and decide whether it's widening-compatible
  with anything.

## Order

1, 2 (a re-test if DuckDB fixes the hang), and 3 before the next Duckstring release. 5 and 6 once 2.0 has
shipped and the quack extension is stable (1.0 with 2.0). 6 shares most of its code with the MotherDuck
engine, so build them together.

## Spike questions (on 2.0 and quack 1.0)

- quack server API: starting it from Python, binding, TLS, how `TOKEN` is checked (one token, several,
  or a verification hook), and whether a client can run anything the sandbox settings don't stop.
- Whether a quack-served instance can be swapped (refreshed) under connected clients.
- Throughput of results over quack against Flight, for the Flock's result transfer.

## Sources

- [A Preview of DuckDB v2.0](https://duckdb.org/2026/08/17/duckdb-20-highlights)
- [Try DuckDB v2.0-dev](https://duckdb.org/2026/09/02/try-duckdb-20-alpha)
