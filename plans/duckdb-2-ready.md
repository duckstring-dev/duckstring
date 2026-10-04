# DuckDB 2.0 ready

Status: **items 1 to 3 built (2026-10-03)**, see "As built" at the end; items 5 and 6 wait for DuckDB 2.0
and quack 1.0. One earlier fix in `1efd8af9`. DuckDB 2.0 is due in the second half of
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
- **Found later (2026-10-04, by the `duckdb-prerelease` CI job): a serialised table reference gained
  `qualified_name.path`, and `json_deserialize_sql` reads that instead of `schema_name`/`table_name`.**
  SQL Ripples (built after the test above) rewrite `source.table` by editing those fields, so on 2.0 the
  rewrite was silently ignored and every SQL Ripple reading a Source failed with `schema "src" does not
  exist`. Fixed: `sql_ripples._rewrite` sets the path too when it's present.
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

- ~~Measure 2.0's wider row-group pruning against the Trickle builder's key-filtered joins~~ Measured
  2026-10-04 (results below): no change in bytes read, about 2x faster wall time; nothing to change.
- Partition-aware queries: little value while Duckstring's Parquet layout isn't Hive-partitioned.
- ~~Recognise `VARIANT` in the schema contract's type handling~~ Done 2026-10-04: decided with the author
  that a change to or from `VARIANT` is breaking in both directions; `is_widening` already behaved that way
  (only `VARIANT` → `VARIANT` passes), so this added the rationale, a test and a line in the upgrades guide.
  DuckDB 2.0 reports the type as `VARIANT` (`STRUCT(a VARIANT)` nested).

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

## As built (2026-10-03): items 1 to 3

**1. Bounds and CI.** `duckdb>=1.5,<2`. CI gained `duckdb-floor` (the suite on `duckdb==1.5.0`) as well as
the planned `duckdb-prerelease` (`pip install --pre --upgrade duckdb` past the ceiling,
`continue-on-error`), since the floor had never been tested. Checked that a Pond environment can't drift:
installing Duckstring into an environment holding `duckdb 2.0.0.dev…` replaced it with 1.5.6. The Python
dependencies guide says so. Also fixed in passing: three tests import fixtures from the `tests` package,
which only resolved under `python -m pytest`, so CI's bare `pytest` failed them; `pythonpath = ["."]`
in the pytest config fixes it.

**2. The chunked-base hang was narrower, and worse, than recorded.** Re-tested on the newest build
(`2.0.0.dev2610011535`, the same one): the repro still fails, but it isn't a hang. The `COPY` loops
forever creating empty `data_N.parquet` files, about 10,000 a second, until the disk is full (it filled
this machine's during the re-test). It only happens when `FILE_SIZE_BYTES` is tiny: 1 and 4 bytes loop,
16 bytes and above (up to a 3M-row table at 1 MB, 4 MB and 10 MB limits) roll over correctly, and 1.5
writes a single file even at 1 byte. Production's 256 MiB threshold never reaches it; the tests that
"hung" set `DUCKSTRING_COMPACT_THRESHOLD=1` to force compaction, which also made the chunk size 1 byte.
So instead of per-chunk `COPY`s, the chunk size is floored at 64 KiB (`dataplane._MIN_CHUNK_BYTES`);
compaction still triggers on the raw threshold. The four affected test files pass on 2.0. Still worth
reporting upstream, with the runaway file creation as the headline.

**Suite results.** The full suite passes on `2.0.0.dev2610011535` (1095 passed, 5 skipped) and on the
floor, `duckdb==1.5.0` (1093 passed; one status test timed out at its 5 s budget under load and passes
on rerun). The `dbt` extra, Postgres egress and MinIO jobs were not run against 2.0.

**3. Unreadable registry.** As planned, in `duck/executor.open_registry`: the errors are all
`IOException`, so the message decides. A newer storage version ("Trying to read a database file with
version number"), "not a valid DuckDB database file", corruption and checksum errors, and WAL replay
failures count; a lock error never does, since moving a file another process holds would be destructive.
The file and its WAL are renamed `registry.duckdb.unreadable-{UTC stamp}`. Verified with a registry
written by 2.0 opened by an executor on 1.5. The dbt executor makes the same check at start and starts
empty (its models are rebuilt every run).

## Measured: row-group pruning for the builder's key filter (2026-10-04)

The builder restricts each side of an incremental join with `(key) IN (SELECT k0 FROM affected_keys)`
(`TrickleBuilder._restricted_join`). Measured on DuckDB 1.5.5 and `2.0.0.dev2610011535` against a 5M-row
Parquet file (`k BIGINT, a BIGINT, b VARCHAR`, about 41 row groups, 181 to 191 MB) served from moto over
HTTP, one fresh connection per query with DuckDB's file caches off, bytes from `EXPLAIN ANALYZE`'s HTTP
stats. The query reads `count(*), sum(a)`, so the `b` column (most of the file) is never fetched.

| File layout | Affected keys | Fetched (both versions) | 1.5.5 | 2.0 dev |
|---|---|---|---|---|
| sorted by `k` | 10 or 1,000 clustered | 0.4% | 43 ms | 30 ms |
| sorted by `k` | 10 spread | 2.9% | 254 ms | 139 ms |
| sorted by `k` | 100 to 100,000 spread | 11.3% (the whole key column) | 985 ms | 505 ms |
| shuffled | any | 14 to 16% | 640 to 1,040 ms | 530 ms |

- **2.0 reads the same bytes as 1.5** for this predicate. Both already push the affected keys into the
  scan (a key range, and for a small set the keys themselves: 10 spread keys skip most row groups), so
  2.0's wider pruning adds nothing here.
- **2.0 is about twice as fast on the same bytes** (async I/O), and the subquery form no longer costs
  more than a literal list (on 1.5 it took twice as long).
- **Layout decides the bytes.** Pruning needs data clustered by the join key. Keys shuffled across row
  groups read the whole key column of every row group whatever the version. A merge table's cold base is
  written ordered by `_duckstring_f` (`dataplane._publish_base_chunks`), so an affected-key read of it
  can't skip anything; ordering the base by key is what `plans/data-plane-clustering.md` proposes,
  and this is evidence for it.

No change to the builder. The benchmark scripts were throwaway (scratchpad); rerun by recreating them from
this description if needed.

