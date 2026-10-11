# Flock coverage

Status: **designed 2026-10-11 with the author; nothing built.** Builds on `plans/flock-motherduck.md` (the
MotherDuck engine and the job-carried settings) and `plans/duckdb-2-ready.md` item 6 (the self-hosted
DuckDB server engine), and replaces the default-engine rule in the former.

## The problem

The Flock is only worth having if most heavy terminals can be dispatched. Today, with Athena the only
engine, most can't. A terminal stays local for one of these reasons:

- **Shapes the compiler can't express.** `AthenaEngine.eligible` refuses `.mutate`, `.aggregate`,
  `.accumulate`, `.sql()`, more than one `.select`, bushy joins and semi/anti joins. Most real Trickles
  use at least one of these.
- **Expressions not on the allow-list.** Only expressions Trino evaluates identically to DuckDB pass
  unchanged: no division, CAST or string functions.
- **Terminals that never reach the hook.** `.aggregate()`'s merge path returns before
  `_dispatch_comprehensive` is called, and a `.sql()` builder is materialised locally when `.sql()` is
  called, before any terminal (`builder.py`, the `_materialised` branch). No engine can take either today.
- **Recomputes that `upgrade` doesn't count.** `_dispatch_comprehensive` treats a run as
  comprehensive-bound only on bootstrap or `ivm=False` (`name not in read_meta`). A coverage miss or a
  source over its `p` threshold also recomputes wholesale, but under `upgrade` it runs locally even when
  it's clearly over the envelope.

Only the second is about two engines disagreeing. The others are unbuilt code, and fixing them carries
no semantic risk, because a join, a filter or a `GROUP BY` means the same thing on every engine; only
the expressions inside them can differ.

## Engine order (decision)

1. **`motherduck`**: runs DuckDB, so the builder's own SQL goes unchanged and nearly everything is
   eligible (`plans/flock-motherduck.md`).
2. **`duckdb-server`** (working name): the same engine against a DuckDB server in the user's own account
   (`plans/duckdb-2-ready.md` item 6). The same coverage, with data staying in the account, which was
   most of Athena's reason to exist.
3. **A distributed SQL engine**: `starrocks`, then `athena`. Only needed past what one large DuckDB
   machine can handle, and always narrower than 1 and 2, because it isn't DuckDB.

With `DUCKSTRING_FLOCK_ENGINE` unset, the engine is the first one in this order whose `enabled()` is
true, else off. This replaces the "motherduck, else athena" rule in `plans/flock-motherduck.md`.
`get_engine` and `engine_secrets` both default to `"athena"` today and change together (the secrets
resolved on the Catchment must be the chosen engine's). An explicit engine, per Pond or Catchment-wide,
is unchanged.

## The StarRocks engine

StarRocks measured well against Trino (the author's benchmark), and it beats Athena structurally for
this workload: no Glue DDL round trips per dispatch, and a conformance gate that can run in CI.

### Service

AWS has no managed StarRocks. The engine uses only the MySQL wire protocol and SQL, so it works against
any deployment; the docs recommend two:

- **CelerData Cloud BYOC** (from the StarRocks authors): deploys into the user's VPC, so data stays in
  the account. Billed per CCU ($0.65 since 2025-07-01) only while clusters run. **The Flock needs a
  dedicated warehouse with auto-suspend**: dispatches are rare, so a warehouse billed while idle would
  cost far more than Athena's per-query billing. The default warehouse can't be suspended on its own.
- **Self-hosted StarRocks in shared-data mode** on EC2 or EKS, with S3 as storage.

### Design: `flock/engines/starrocks.py`

- **Config**: `DUCKSTRING_FLOCK_STARROCKS_{HOST,PORT,USER,WAREHOUSE,SCRATCH}`; `SECRETS =
  ("STARROCKS_PASSWORD",)`. Client `pymysql`, imported lazily, in a `duckstring[starrocks]` extra.
  `enabled()` needs host, user and scratch.
- **Staging**: as Athena, each leaf's current state is copied to `{scratch}/stage/{job}/src{i}` as
  Parquet. StarRocks reads it with `FILES()`, which infers the schema from the Parquet, so there is no
  `CREATE EXTERNAL TABLE`/`DROP` and no type map.
- **Result**: `INSERT INTO FILES("path" = "{scratch}/stage/{job}/out/", "format" = "parquet") SELECT …`
  (StarRocks 3.2+), read back with `read_parquet`, then `conform` as for any engine.
- **S3 access**: StarRocks uses its own instance role (`aws.s3.use_instance_profile`); no keys in SQL.
- **Eligibility**: whatever the shared compiler (part 1) can express, with the expression rules of parts
  2 and 3. The allow-list starts at `equivalence.BASE_NODES` and grows only from measurement.

### Spike

- Does a query sent to a suspended CelerData warehouse resume it, or must the engine call the resume API
  and wait? Either way, how long, since the wait counts against the dispatch timeout, and under `upgrade`
  an OOM fail-up dispatches in the middle of a run.
- Divergences to measure first: `/` (MySQL-style true division is expected, but unverified), division by
  zero, decimal result precision on `*` and `+` past 38 digits, CAST at .5, string comparison and
  trailing spaces.
- Throughput of `INSERT INTO FILES` against fetching the result over the MySQL protocol for small results.

## 1. Compiler coverage

A shared compiler, `flock/compile.py`, grown from `athena._compile_select`, emits one SELECT for a SQL
engine. A small dialect object carries what differs (identifier quoting, join keywords, how semi/anti
are written: `EXISTS`/`NOT EXISTS` where the dialect has no semi-join syntax). Athena and StarRocks
both use it.

- **Joins**: every `how`, any shape. A non-leaf right operand becomes a nested subquery. Columns are
  carried as the builder's leaf-qualified `"alias.col"` names, so the builder's own resolution rules
  (`_resolve_col`, `_star_output`) decide the output and the compiler only renders them.
- **Pipeline**: `.filter`/`.mutate`/`.select` in call order as nested subqueries, mirroring
  `_pipeline_sql`, so a filter can read a mutated column and several selects compose.
- **`.aggregate()`**: dispatch the **accumulator rebuild**, not the aggregate's input. Shipping the
  joined input back would move the largest relation in the plan; `_agg_rebuild`'s accumulators are one
  row per group. The engine runs the `GROUP BY` that produces the raw accumulators (`_a_cnt`, `_a_sum`,
  `_a_min`/`_a_max`, centred `_a_m2` from `var_pop · n`, the co-moment and weighted columns) and the
  Duck installs them and derives the published columns as it does today. This needs a hook in the
  aggregate path. Each accumulator is an expression subject to parts 2 and 3: a DECIMAL sum is exact,
  a DOUBLE sum differs in the last bits (`float_order`). argmin/argmax and the semigroup kinds come
  later.
- **The `upgrade` gap**: count a coverage miss and an over-`p` source as comprehensive-bound. The builder
  only learns this inside `_compute`, so the hook moves to the point where `_compute` has decided to
  recompute wholesale, before it runs `_full_join`.
- **`.sql()`**: only DuckDB engines (MotherDuck, duckdb-server) can take arbitrary DuckDB SQL. Offering
  it means dispatching when `.sql()` is called rather than at the terminal; that belongs with the
  MotherDuck engine. SQL engines may take it later through translation (part 2), gated like any other
  expression.
- **Always local**: `.accumulate()` and `agg.reduce`, which are Python folds, and every incremental run,
  which is small by construction.

## 2. Translation, trusted only where measured

Today's rule (`equivalence.py`: pass expressions through unchanged, never rewrite) limits SQL engines
to functions that happen to behave identically. Instead, translate each expression with sqlglot
(`read="duckdb"`, `write=` the engine's dialect) and allow a construct only when its **translation**
has been measured to agree. sqlglot is already present through the `lineage` extra, and it's a
maintained translator, so this isn't the second implementation of DuckDB's semantics the old rule was
guarding against. `equivalence.py`'s docstring changes to say so.

Trying sqlglot 28.5.0 (2026-10-11) shows why translation can't be trusted unmeasured:

| DuckDB | Trino output | Verdict |
|---|---|---|
| `a / b` | `CAST(a AS DOUBLE) / NULLIF(b, 0)` | correct; fixes the division case (check divide by zero) |
| `x // 2` | `CAST(CAST(x AS DOUBLE) / 2 AS INTEGER)` | wrong: Trino rounds 3.5 up to 4, and BIGINT loses precision past 2^53 |
| `CAST(x AS INTEGER)` | unchanged | still differs at .5 |
| `list_sum(l)` | unchanged | fails on the engine, so falls back safely, but after a wasted round trip |

- The allow-list keeps its shape (nodes and functions per engine) but means "translated and measured".
  The conformance tests run the **translated** SQL on both engines and compare post-`conform` values.
- `eligible()` refuses anything the dialect passes through untranslated that the engine doesn't have,
  instead of letting it fail remotely.
- Translations depend on the sqlglot version: pin a range in the extras, and re-run the conformance
  tests on a sqlglot bump.

## 3. Named acceptances, per Pond

Some differences are known, bounded and harmless for many datasets. Let a Pond accept them by name:

```toml
[flock]
mode = "upgrade"
accept = ["float_order", "half_rounding"]
```

- **The vocabulary** starts with `float_order` (floating-point SUM/AVG and DOUBLE accumulators can differ
  in the last bits, because a parallel engine adds in a different order; DuckDB isn't bit-stable across
  thread counts either) and `half_rounding` (CAST to an integer at exactly .5). Each name is documented
  with its consequence. New names are added deliberately, each with a measured example.
- **Each engine maps constructs to kinds**: per engine, an allowed set (measured equal), a map from each
  acceptance kind to the constructs that risk it, and everything else refused. `eligible()` passes a
  construct when it's allowed or its kind is accepted.
- **Never acceptable**: a different value class. Integer division returning 3 for 3.5, an error where
  DuckDB returns NULL, or a type change that `conform` would mask is a wrong answer, not a tolerance, and
  no name may cover it.
- **Per Pond**, because whether a last-bit float difference matters depends on the data (finance versus
  product analytics), and the Pond author knows it. It follows the Flock mode's path: declared in
  `pond.toml`, stored on `pond_version`, overridable in `pond_duck` (`flock_accept`, a migration) via
  `duckstring duck set --accept`, and sent on the job as `DUCKSTRING_FLOCK_ACCEPT` (a posture key in
  `_POSTURE_KEYS`). Default: none.

## 4. Sampled verification

The safety net for parts 2 and 3. After a dispatch to a non-DuckDB engine, before `conform`'s result is
used:

1. Take a random sample of output keys (`DUCKSTRING_FLOCK_VERIFY_KEYS`, default 1,000).
2. Recompute only those keys locally. The builder already restricts joins to a key set
   (`_restricted_join`); the sample is pushed to the leaves through column lineage, using the output PK's
   **exact** source columns (`trickle/lineage.py`).
3. Compare the local rows with the engine's rows for the same keys, in both directions, so missing,
   extra and wrong rows are all caught. Accepted kinds are compared with their tolerance (for
   `float_order`, a relative epsilon on DOUBLE columns).
4. A mismatch rejects the result: local compute, counted as a failed dispatch, with `last_error` naming
   how many keys disagreed.

Limits, stated in the docs: a sample catches systematic differences (wrong on every row, such as a
division) but can miss rare ones (values exactly at .5). It complements the allow-list and doesn't
replace it. When the output PK isn't exactly traceable to leaf columns, the sample can't be pushed down,
so verification is skipped and logged; whether a Pond with acceptances should then refuse to dispatch
is decided during the build. On by default for SQL engines, off for DuckDB engines.

## Order

1. The MotherDuck and duckdb-server engines (`plans/flock-motherduck.md`, `plans/duckdb-2-ready.md`
   item 6), with the new default-engine rule. These give the most coverage.
2. The `upgrade` gap (small) and the shared compiler (part 1). Athena benefits immediately.
3. The StarRocks engine, with conformance in CI.
4. Translation (part 2).
5. Acceptances and verification together (parts 3 and 4): verification is the net that makes
   acceptances safe to offer.

## Tests

- `tests/test_flock_compile.py`: every builder shape compiled per dialect, run against DuckDB itself as a
  stand-in engine (the compiled SQL is valid DuckDB for the DuckDB dialect), compared with the local
  result. Offline.
- `tests/test_flock_starrocks.py`: eligibility, staging and result paths against a fake connection;
  the default-engine order.
- `tests/test_flock_starrocks_conformance.py`: translated constructs on both engines, against
  `starrocks/allin1-ubuntu` in Docker, in a CI job next to the MinIO `object-store` job (skipped locally
  without the endpoint). Athena's conformance test switches to translated SQL.
- Acceptances: a Pond with and without `float_order` dispatches or stays local on the same terminal; the
  setting travels on the job.
- Verification: an engine fake that corrupts one column on every row is rejected; one that drops rows is
  rejected; a correct one passes.

## Docs

- The Flock guide ("Offloading large recomputes", from `plans/flock-motherduck.md`) gains the engine
  order, StarRocks setup (CelerData BYOC with a dedicated auto-suspending warehouse; self-hosted
  shared-data), what each engine accepts, acceptances and verification.
- `pond_toml.md` (`[flock] accept`), `cli/duck.md` (`--accept`), `environment.md` (StarRocks variables,
  `DUCKSTRING_FLOCK_ACCEPT`, `DUCKSTRING_FLOCK_VERIFY_KEYS`, the default rule).
- CLAUDE.md's Flock section: the engine order, the translated allow-list, acceptances and verification.

## Sources

- [CelerData: usage and billing](https://docs.celerdata.com/BYOC/docs/category/usage-and-billing/)
- [CelerData: manage warehouses (auto-suspend)](https://docs.celerdata.com/BYOC/docs/cluster_management/warehouse/manage_warehouses)
- [CelerData: resume a warehouse (API)](https://docs.celerdata.com/BYOC/docs/API/actions/warehouses/create_and_manage_warehouses/resume_warehouse)
- [StarRocks: FILES()](https://docs.starrocks.io/docs/sql-reference/sql-functions/table-functions/files)
- [StarRocks: unload using INSERT INTO FILES](https://docs.starrocks.io/docs/unloading/unload_using_insert_into_files)
