# Repositioning: feature changes

Status: **§1 skipped, §2 agreed (2026-10-04) and not built; §3.4 superseded by
`plans/flock-motherduck.md`.** The engineering half of `plans/documentation-rewrite.md`. Three features, each
motivated by a friction the rewrite would otherwise have to write around. None is load-bearing for the
documentation work — the docs can ship without all three — but each removes a paragraph of apology.

| # | Feature | Motivation | Size |
|---|---|---|---|
| 1 | Run instrumentation | Demos stop needing to be big or slow to be legible | S |
| 2 | Native `.sql` Ripples | "Point it at your SQL folder" as the on-ramp | S–M |
| 3 | MotherDuck support | Ecosystem citizenship; and MD is the one Flock engine with no dialect risk | M–L |

---

## 1. Run instrumentation: skipped (2026-10-04)

The idea was to record rows read, written and changed per Ripple run and print a "did the minimum"
summary after each run. The author decided against it for now: printing it everywhere is more noise than
use, a CLI `--stats` view needs more design than it would get here, and the UI shouldn't change for it.
Revisit only with a fresh design.

---

## 2. SQL Ripples

Agreed with the author on 2026-10-04. Replaces the earlier `[pond] sql = "models/"` design, which
inferred dependencies.

**The problem.** A reader with a folder of SQL has to wrap each query in a decorated Python function, or
adopt dbt. Both are a hurdle for someone who just wants their queries to run as a pipeline.

**The rule: everything is explicit.** Nothing about a Ripple is inferred from its SQL. Every SQL Ripple
is declared in `pond.toml`, with its parents, the Source tables it reads, and how it writes.

### 2.1 Declaration

```toml
[sources]
transactions = "1.0.0"
products = "1.0.0"

[ripples.sale_line]
sql = "sql/sale_line.sql"
reads = ["transactions.order_line", "products.product"]

[ripples.daily_sales]
sql = "sql/daily_sales.sql"
parents = ["sale_line"]
write = "merge"
pk = ["day", "product_id"]

[static.regions]
path = "data/regions.csv"
```

`[ripples.NAME]`:

| Key | Default | Meaning |
|---|---|---|
| `sql` | required | Path, relative to the Pond's directory, of a file holding one `SELECT`. The Ripple writes one table, named `NAME`. |
| `parents` | `[]` | Ripples in this Pond (SQL or Python) that must finish first. |
| `reads` | `[]` | Source tables the query reads, as `source.table`, each from a Source in `[sources]`. |
| `write` | `"overwrite"` | `"overwrite"` (`write_table`), `"merge"` (`merge_table`) or `"append"` (`append_table`). |
| `pk` | | Required for `merge`; optional for `append` (as `append_table`'s `pk`). A string or a list. |
| `always_run` | `false` | As `@ripple(always_run=True)`. |

`[static.NAME]`: a file shipped with the Pond's code (CSV, Parquet or JSON, by extension), with `path`
relative to the Pond's directory. Every Ripple, SQL or Python, sees it as a read-only table `NAME`.
Static tables take no part in ordering, so they're never listed in `parents` or `reads`. A changed file
arrives with a redeploy and is used from the next run; it doesn't trigger a run by itself. Data from
outside the Pond's code (`read_parquet('s3://...')`, an attached database, an API) belongs in an Inlet
and is not declared here.

Names: a Ripple name and a static name share the Pond's table namespace, so a clash is a deploy error,
as is a static name clashing with a table a Python Ripple writes (found at its first run).

### 2.2 Mixing with Python

A Pond can have SQL Ripples and `@ripple` functions together. `parents` on either side can name the
other kind: `@ripple(parents=...)` accepts names (strings) as well as function references. A dbt Pond
still can't have either.

The Ripple list is built in three places today, each mapping parent function references to names on its
own: deploy discovery (`discover.run`), the Duck (`duck/executor.load_topology` / `_import_ripples`) and
the local runner (`local/runner.py`). `load_topology` and the runner silently drop a parent they can't
resolve (`if p in func_to_name`), which loses an ordering edge without a word. Replace all three with one
function in `core.py` that imports the Python entrypoint (if present), adds the SQL Ripples from
`pond.toml`, resolves every parent to a name, and raises on an unknown parent, a duplicate name or a
cycle. Each SQL Ripple becomes an ordinary Ripple callable:

```python
def run(pond):
    for ref in reads:
        pond.read_table(ref)            # registers the Source table for the query (see 2.3)
    rel = pond.con.sql(text)
    if write == "merge":
        pond.merge_table(name, rel, pk=pk)
    elif write == "append":
        pond.append_table(name, rel, pk=pk)
    else:
        pond.write_table(name, rel)
```

Executor, freshness, retries, contracts, lineage, Puddles and the Flock all work unchanged. The
`pond.trickle(...)` builder stays Python-only; a SQL user wanting an incremental join writes a merge
over the join (comprehensive, correct), or a Python Ripple.

### 2.3 How Source and static tables are named in the SQL

`Pond.read_table` registers a foreign table as a view with the table's bare name. SQL users will
naturally write `transactions.order_line`, matching the catalog, so for SQL Ripples register each `reads`
entry in a schema named after the Source (`transactions.order_line`), on the Ripple's own connection.
Decide in the build whether these are temporary (preferred: nothing lands in the registry) and whether
the bare name stays available too. Static tables are views over the bundled file, registered under
their bare name on every Ripple's connection.

### 2.4 The reference check

At deploy, each SQL Ripple's query is parsed by DuckDB (`json_serialize_sql`) and its base-table
references collected, excluding the query's own CTE names. Table functions (`read_parquet(...)`) aren't
base tables and are ignored. Every remaining reference must be one of:

- a Ripple in `parents`, or one further up its ancestry;
- a `reads` entry;
- a `[static]` table;
- the Ripple's own table (a deliberate read of its previous output).

Anything else fails the deploy (a 422 naming the Ripple and the table), so a forgotten `parents` entry
can't make a Ripple silently read the previous run's table. The declaration stays the source of truth:
the check never adds an edge.

Python Ripples get a runtime version of the same check, on the reads the Pond handle brokers
(`read_table`, `read_delta`, `pond.trickle(...)` on an own table): reading a sibling's table that isn't an
ancestor fails the Ripple with the same message. SQL a Python Ripple runs directly on `pond.con` isn't
visible to it; say so in the docs. Wrapping the connection to see it was judged too invasive.

### 2.5 Touch points

- `core.py`: the shared Ripple collection (2.2); `@ripple(parents=...)` accepting names; the runtime
  sibling-read check on brokered own-table reads.
- `discover.py`, `duck/executor.py`, `local/runner.py`: use the shared collection.
- Deploy (`routes/deploy.py`): the reference check (2.4); `[ripples]`/`[static]` validation (files
  exist, `pk` present for merge, `reads` sources declared, names unique); 422s with the reason.
- The Pond handle or executor: register static tables for every Ripple; SQL Ripples' `reads` views.
- Puddles: `pond run` works unchanged (same collection, same handle). `[static]` files are in the
  project directory, so they're there locally too.
- `.pondignore`: `[static]` and `sql` files must not be excluded (check `DEFAULT_PATTERNS`).
- `cli/pond.py demo`: a `--sql` demo, the default pipeline written as SQL files.
- Docs: `pond_toml.md` (`[ripples]`, `[static]`), a Building Ponds guide page for SQL Ripples, the
  decorators reference (`parents` takes names), and the Ripples concept page's mention of SQL.

### 2.6 Tests

Declaration validation (each 422); the reference check (an undeclared sibling, Source or file fails the
deploy; CTEs, table functions, self-reads and ancestors pass); mixed Ponds in both directions; each write
mode; static tables in SQL and Python Ripples; the Python runtime check; the shared collection raising on
an unknown parent where it used to drop it; a deployed-Duck e2e of the SQL demo.

---

## 3. MotherDuck support

Positioning context: the ecosystem is **DuckDB the engine, MotherDuck the compute service, Duckstring
the platform**. Being a good citizen of that ecosystem is worth real engineering, and one of the layers
below is not merely integration but the best version of a feature we already have.

Five layers, increasing in cost. The recommendation is to build 1, 2 and 4, defer 3, and treat 5 as
probably-never.

### 3.0 Shared groundwork: connection and credentials

Everything below needs one thing: a DuckDB connection with MotherDuck attached.

```sql
ATTACH 'md:mydb';   -- requires the motherduck extension + a token
```

- **Token** — `motherduck_token`, resolved through the existing credential machinery
  (`egress/credentials.resolve`): `${env:MOTHERDUCK_TOKEN}` or `${secret:MOTHERDUCK_TOKEN}` against the
  write-only catchment secret store. Never in `pond.toml`, never in argv, never in a URI that gets
  logged — the same discipline `egress/object_store.py` applies to S3 keys, including masking the
  `CREATE SECRET` error so it cannot echo the token.
- **Extension install needs network.** Same precedent as the Iceberg plane (`iceberg_plane` notes that
  an offline Catchment uses `parquet`): an unreachable extension repository must degrade, not fail.
- **A gate, like `cloud_enabled`.** `catchment_setting` gains `motherduck` state, and the UI/CLI report
  whether MD is configured. Reuse the pattern in `catchment/cloud.py` rather than inventing a second
  one.

### 3.1 MotherDuck as a Source (small)

**Already works, undocumented.** A Ripple can `ATTACH 'md:…'` on `pond.con` and read. Zero features
required; it needs a guide page and a demo, and it is the cheapest possible ecosystem story.

The first-class version — declaring MD as an external source in `pond.toml` so reads are registered and
appear in lineage — is worth doing after the egress driver, because it needs a place in the source
model that does not exist yet (`[sources]` names Ponds, not foreign systems). Until then a `@ripple`
Inlet that attaches and reads is the honest answer, and it is what a user would write anyway.

### 3.2 MotherDuck as an egress target — a `md://` Spout (small, do first)

The natural fit, and the seam is already there. `egress/base.get_egress:78` is a scheme registry;
`destination.KNOWN_SCHEMES` gains `md`.

**Why this is the cheapest real integration:** it is `egress/postgres.py` with the dialect problem
deleted. That driver's whole approach is "ATTACH the destination and run plain DuckDB SQL against
attached tables". MotherDuck *is* DuckDB, so the same design applies with less translation.

```
Capabilities(supports_delta=True, supports_delete=True, transactional=True, mirrors_layout=False)
```

- **`ensure`** — `CREATE TABLE IF NOT EXISTS` in the attached MD database, PK from the Trickle sidecar.
- **`write_full`** — `CREATE OR REPLACE TABLE md_db.schema.tbl AS SELECT …`.
- **`apply_delta`** — the Postgres pattern exactly: delete the changed ∪ removed keys, re-insert the
  present rows, in one transaction, with the watermark written to `_duckstring_egress` **in the
  destination** in the same transaction. That is what buys exactly-once across crashes, and it works
  here for the same reason it works there.
- **`test_connection`** — ATTACH + `SELECT 1`, error sanitised so it cannot carry the token. This backs
  the UI's *Test* button (`POST /api/ponds/{name}/spouts/test`).
- The **transactional-PK gate** (`Driver._assert_transactional_pk`) applies unchanged: only a merge
  Trickle has a pk, so only a merge Trickle gets a transactional MD sink.

Destination URI: `md://database/schema/table` with the token by reference, e.g.
`md://analytics/public?token=${secret:MOTHERDUCK_TOKEN}`. Follow `object_store.py`'s rule that the
target URI carries no credential query, so a failure message cannot leak one.

Tests mirror `tests/test_egress_file.py`; a real-MD test is opt-in behind an env var, the way
`DUCKSTRING_TEST_S3` gates the MinIO suite.

### 3.3 MotherDuck as the data plane (defer — scoped here for completeness)

Publishing Pond output *into* MotherDuck as the cross-Pond interchange layer, replacing parquet/Iceberg
on S3.

This is a genuine option and it is a bigger job than it looks, because `dataplane.DataPlane` is
file-shaped throughout:

- `export(con, data_dir, mode, f)`, `read_select(data_dir, table, as_of)`, `list_tables(data_dir)`,
  `table_path`, and `files_for` — the last of which exists specifically so ducts and draws can ship raw
  parquet parts between Catchments.
- The Trickle tiers are **directories of immutable parts** (`{table}/{f}.parquet`, `__band/`,
  `__base/`). Their incrementality (`O(change)` publish, idempotent-by-name transfer, retention by part
  pruning) is expressed as file operations.

A `MotherDuckDataPlane` would map tiers onto tables rather than part directories. Some of that is
*better* — an `INSERT` of the delta is still O(change) and needs no reconciliation pass — but
`files_for` has no analogue, so cross-Catchment draws (`routes/draw.py`, `poller._land_transfer`) would
need an entirely different transfer path, and the as-of read seam would need re-deriving.

**Recommendation: defer.** §3.2 puts the data in MotherDuck for the BI use case, which is the reason
anyone asks for this, without restructuring the interchange layer. Revisit only if someone wants MD as
the *primary* store rather than a destination — and note that §3.4 becomes dramatically better if they
do, because the Flock's staging step disappears entirely.

### 3.4 MotherDuck as a Flock engine (the interesting one)

**This is not integration — it is the best version of a feature we already have.**

The Flock's hardest problem is that its engines are not DuckDB. `flock/equivalence.py` and the
allow-lists in `flock/engines/athena.py` exist entirely because Trino disagrees with DuckDB in ways
that would publish wrong data: `7/2` is `3` on Trino and `3.5` on DuckDB, and `conform` would faithfully
cast that `3` to DOUBLE and publish `3.0` — the right type carrying the wrong value. `CAST(2.5 AS
INTEGER)` is 2 locally and 3 on Athena. String functions are unverified. The result is Athena's `eligible()`
refusing `.mutate()`, `.aggregate()`, `.accumulate()`, `.sql()`, multiple `.select()`s, non-left-deep
shapes, semi/anti joins, division, CAST and string functions — most of the builder surface.

**MotherDuck runs DuckDB.** Every one of those restrictions is a Trino artifact, and none of them
applies.

#### What the engine looks like

`flock/engines/motherduck.py`, implementing the same `FlockEngine` protocol (`flock/__init__.py:157`):

- **`eligible(builder)`** → `None` for essentially everything. No `equivalence.unproven` call, no
  left-deep check, no op restrictions. The remaining reasons to refuse are *physical*, not semantic:
  the data is not reachable from MD (see staging), or MD is not configured.
- **`estimate_rows(builder)`** — unchanged from Athena's: sum `count(*)` over the source leaves.
- **`dispatch(builder, out_pk)`** — and here the whole `_compile_select` layer disappears. The builder
  already produces DuckDB SQL; `builder._full_join().sql_query()` with leaf references rewritten to
  their MD names *is* the query. Athena needed a compiler because it needed a different dialect. There
  is no dialect gap to cross.
- **`conform`** still runs. **Do not skip it.** It should never reject, and the day it does we want to
  know — that is a version-skew signal (below), not a nuisance. It costs one projection.

Everything else in `flock/__init__.py` — the `off|upgrade|always` ladder, `_min_rows` derived from the
Duck's memory cap, `_probe_local` and the OOM fail-up, the dispatch counters shipped per Ripple Run,
the never-load-bearing fallback contract — works unchanged. That is the point of the seam.

#### Staging: where does MD read from?

Three cases, and the engine should decide by inspecting the data root:

1. **Data root is S3.** MotherDuck reads S3 directly. "Staging" is pointing MD at the same parquet the
   Duck reads — a `CREATE VIEW … AS SELECT * FROM read_parquet('s3://…')` per leaf, or MD's own secret
   configured once. Far cheaper than Athena's `COPY` + `CREATE EXTERNAL TABLE` + Glue cleanup dance.
2. **Data root is MotherDuck** (§3.3, if it ever lands). Nothing to stage; the query runs entirely
   server-side. This is the ideal shape and the strongest argument for revisiting §3.3.
3. **Data root is local disk.** Uploading gigabytes to dispatch is not a win. `eligible()` should return
   a reason ("local data root — nothing for the engine to read") and take the local path. This is the
   laptop case and refusing is correct.

#### The real equivalence risk: version skew, not dialect

Worth stating loudly because a naive plan misses it. MotherDuck removes the *dialect* problem entirely
and replaces it with a smaller, sharper one: **MD may run a different DuckDB version than the Duck.**
DuckDB's own behaviour changes between releases; two DuckDBs of different vintages are not guaranteed to
agree any more than DuckDB and Trino are — just far more likely to.

Mitigations, in order:

- **Record both versions.** `SELECT version()` locally and on the attached MD connection at engine
  construction; log the pair on the first dispatch, and attach it to the dispatch counters.
- **Warn on mismatch**, don't refuse — a minor-version difference is almost always fine and refusing
  would make the engine useless in practice.
- **Let `conform` be the backstop.** It already fails closed and already counts a rejection as a failed
  dispatch, so a genuinely divergent MD shows up on `/metrics` rather than in published data. This is
  the same safety net, doing the job it was built for.
- **A conformance test**, mirroring `tests/test_flock_athena_conformance.py`: run candidate expressions
  on both, compare post-conform, skip unless MD is configured. Its purpose is inverted, though — the
  Athena test exists to *earn* additions to an allow-list; this one exists to *detect* a version skew
  that breaks an assumption of equality. Worth saying so in the test's docstring, because someone will
  otherwise copy the Athena framing and re-introduce a needless allow-list.

#### What this unlocks

A Pond whose comprehensive recompute exceeds the box can dispatch **any** builder shape — aggregates,
accumulations, `.sql()` escape hatches, bushy joins — to a bigger DuckDB, and get back an answer that
is DuckDB's by construction rather than by measurement. That is a materially better story than the
Athena engine can offer, and it is mostly *deletion* relative to Athena's implementation.

#### Deliberately out of scope for v1

Pushing the *whole* terminal server-side — the merge/diff/publish as well as the read side — is
tempting, since MD is DuckDB and can run `trickle_io`'s SQL. It would also break the Flock's standing
invariant that the Duck keeps the semantics, and it turns a dispatch into a distributed write path with
its own failure modes. Keep v1 to the existing contract: engine does the heavy read, Duck merges and
publishes. Revisit as its own plan.

### 3.5 MotherDuck as Duck compute (probably never)

Running an entire Duck against MD — the registry itself living in MotherDuck rather than on local disk.
The `duck_pool` provider seam (`provider` ∈ `fargate`/`ec2`, migration `023`) looks like the place, but
it is the wrong axis: a launcher decides *where a process runs*, and this decides *where the registry
lives*. The registry is a read-write working database hammered by every Ripple in a run; moving it to a
network service changes the latency profile of the whole executor.

§3.4 gets the benefit — big compute, on demand, with DuckDB semantics — without any of that. Record
this as considered and rejected, not overlooked.

---

## 4. Sequencing (revised 2026-10-04)

1. **SQL Ripples** (§2). Next to build.
2. **MotherDuck Flock engine**: built per `plans/flock-motherduck.md` once there's an account for its spike.
3. **MotherDuck groundwork + `md://` Spout** (§3.0, §3.2): also needs an account; after the engine.
4. Skipped: run instrumentation (§1). Deferred: MD data plane (§3.3), MD as Source first-class (§3.1), MD
   compute (§3.5).

## 5. Decisions (2026-10-04)

- SQL Ripples are declared in `pond.toml` (`[ripples.NAME]`), fully explicit, with no dependency
  inference; sqlglot isn't involved.
- SQL and Python Ripples can be mixed in one Pond.
- The reference check applies to every SQL Ripple at deploy, and to Python Ripples' brokered reads at run
  time.
- Static content shipped with the Pond is declared in `[static.NAME]`.
- Still open: when to use SQL Ripples rather than dbt mode needs one clear sentence in the docs, decided
  before the docs are written. And the MotherDuck destination URI shape (§3.2), when that's built.
