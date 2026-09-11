# Repositioning: feature changes

Status: **proposed.** The engineering half of `plans/documentation-rewrite.md`. Three features, each
motivated by a friction the rewrite would otherwise have to write around. None is load-bearing for the
documentation work — the docs can ship without all three — but each removes a paragraph of apology.

| # | Feature | Motivation | Size |
|---|---|---|---|
| 1 | Run instrumentation | Demos stop needing to be big or slow to be legible | S |
| 2 | Native `.sql` Ripples | "Point it at your SQL folder" as the on-ramp | S–M |
| 3 | MotherDuck support | Ecosystem citizenship; and MD is the one Flock engine with no dialect risk | M–L |

---

## 1. Run instrumentation

**The problem.** A run that finishes in 40 ms cannot demonstrate anything by its duration, and the
current output gives nothing else — `pond run` prints per-Ripple wall-clock only, and `ripple_run`
records `started_at`/`finished_at` but no volumes. So a demo has to be either big or artificially slow
to be legible, which is the trade-off `documentation-rewrite.md` §5 is trying to escape.

**The claim.** Duckstring's central assertion is that it does the *minimum*. Measuring the minimum on
the reader's own run is a stronger demonstration than any duration, and it reads identically well at
50 MB and 50 GB:

```
4 Ponds · 1 ran · 3 skipped (no change)
4,218,004 rows standing · 25,000 changed · 0.6% recomputed
elapsed 71ms
```

No comparison to anyone, no number we assert, and the demo stops needing scale.

### 1.1 Where the numbers come from

**Piggyback on the lineage recorder, don't build a parallel path.** `core.Pond` already brokers every
read and write a Ripple makes and records them for lineage — `_record_read` (`core.py:409`),
`record_lineage_write` (`core.py:412`), drained per Ripple Run by `take_lineage` (`core.py:417`). That
is exactly the set of events we want counted, it is already shipped on the `ripple` event, already
persisted per run, and already documented as never failing a run. Widening those records from
`(source, table)` to `(source, table, rows)` gets the whole feature almost for free.

Cost of counting, per path:

- **Trickle writes** — free. `apply_zset` already consolidates the delta before writing, so the change
  row count is known without an extra pass. `merge_table`/`append_table` likewise.
- **Overwrite writes** (`write_table`) — a `count(*)` on the just-written table. Cheap: DuckDB answers
  from table metadata, and the relation is materialised at that point anyway.
- **`read_delta`** — free, same reason as the Trickle write.
- **`read_table`** — a `count(*)` over the source. Parquet-backed reads answer from file statistics; a
  merge Trickle's reconstruction does not, so this one is worth gating (see below).

**Gate the non-free counts.** `read_table` on a reconstructed merge main is a real query. Either count
only what is free and report the rest as unknown, or put the expensive counts behind an opt-in. I'd
start with free-only: the interesting numbers (delta size, rows written, skipped Ponds) are all in the
free set, and "rows standing" can come from the sidecar/`consolidated_count_select` rather than a scan.

### 1.2 Transport and storage

- `duck/core.Event` gains `stats: dict | None` on the `ripple` event, beside the existing `changed`
  and the Flock counters — same shape, same idempotency story.
- `duck/executor.RippleExecutor.submit`'s `on_done(name, started, finished, lineage)` gains the stats
  (or lineage carries them, since they are the same records).
- Migration `029_run_stats.sql`: `ripple_run` gains `rows_read`, `rows_written`, `rows_changed`
  (nullable — absent for old rows and for ungated counts). `pond_run` needs nothing; the Pond-level
  numbers are a `SUM` over its Ripple rows, and "skipped" is already derivable from the no-change pass
  rows `_record_pass` writes.
- `Driver._record_lineage` is the natural place to persist them — it already runs on the same event.

### 1.3 Surfaces

- **`duckstring pond run`** (`local/runner.py` → `cli/pond.py:265`) — the local path has no Duck, but it
  uses the same `Pond` handle, so it gets the same numbers with no extra plumbing. This is the
  quickstart's surface and the most important one.
- **One-shot triggers** (`tap`/`pulse`/`wake`/`force`) — these already hold the live status view open
  until the pipeline settles. Print the summary when it closes.
- **`duckstring status`** — last-run volumes per Pond.
- **Web UI** — `RunDetail` shows them per attempt; the store already polls `/api/runs`.

### 1.4 Risks

- **Never let a counter fail a run.** Same contract as lineage: wrap and drop. A `count(*)` that throws
  must produce a missing number, not a failed Ripple.
- **Don't make the free path expensive.** The rule is that instrumentation may read metadata but must
  not add a scan. Anything that would is either gated or reported as unknown.

---

## 2. Native `.sql` Ripples

**The problem.** The quickstart's first step asks a reader holding a folder of SQL to put it inside a
decorated Python function. For the migration audience that is the first hurdle and it is avoidable.
dbt-mode already offers a SQL-only path, but it requires adopting dbt — a large ask for someone who
just wants to see their query run.

**The shape.** `pond.toml [pond] sql = "models/"`, mutually exclusive with `dbt_project` and with
`@ripple` code, exactly as dbt-mode is. One `.sql` file per output table; the file stem is the Ripple
name and the table name.

### 2.1 Why this is much smaller than dbt-mode was

dbt-mode needed its own executor (`duck/dbt_executor.py`) because **dbt owns its own connection** —
two open DuckDB connections to one registry file conflict, so `DbtExecutor` holds none and serialises
transient ones. A `.sql` Ripple has no such problem: it is just SQL run on the Pond's existing
connection.

So the whole runtime half collapses. Rather than a new executor, **synthesise the ripple callable**:

```python
def _sql_ripple(text, name, mode, pk):
    def run(pond):
        rel = pond.con.sql(text)
        if mode == "merge":
            pond.merge_table(name, rel, pk=pk)
        elif mode == "append":
            pond.append_table(name, rel, pk=pk)
        else:
            pond.write_table(name, rel)
    return run
```

That is an ordinary Ripple. `RippleExecutor`, the freshness model, retries, contracts, lineage,
Puddles and the Flock all work unchanged, because nothing downstream of discovery can tell the
difference.

### 2.2 Discovery and dependencies

New module `sql_mode.py`, mirroring `dbt_mode.py`'s role but far thinner. It emits **the same ripple-row
shape** everything else already consumes — `{"func", "name", "parents", "always_run"}` — which is what
makes the rest free (`dbt_mode.manifest_to_ripples:97` documents that contract).

Dependency resolution, in precedence order:

1. **Explicit** — a leading `-- depends: orders, catalog` comment. Always honoured, always wins.
2. **Inferred** — parse the statement with **sqlglot** (already an optional dep for column lineage,
   the `duckstring[lineage]` extra) and collect table references. A reference resolves to a sibling
   Ripple if it matches another file's stem; to a cross-Pond read if it is `source.table` and `source`
   is declared in `[sources]`; otherwise it is left alone (a CTE, a function, an attached database).

Inference should require sqlglot rather than regex — guessing dependencies wrong produces a silently
mis-ordered pipeline, which is far worse than an import error. If sqlglot is absent, require the
explicit form and say so.

### 2.3 Cross-Pond reads

`Pond.read_table` already registers a foreign Source's table as a temp view named after the table
(the convention documented in CLAUDE.md, and the reason replacement scans are banned). So a `.sql`
file referencing `transactions.order_line` is served by pre-registering its resolved sources before the
statement runs — the same move `dbt_mode.materialize_sources:126` makes, minus dbt's schema mapping.

### 2.4 Materialisation modes

Frontmatter comments keep the Trickle surface reachable without Python:

```sql
-- depends: orders, catalog
-- materialize: merge
-- pk: order_id
SELECT ...
```

`overwrite` (default), `merge`, `append`. This is worth having in v1: without it, sql-mode is an
overwrite-only ghetto and the docs would have to tell readers to leave it for anything incremental —
which contradicts the "incremental is the default" beat.

The `pond.trickle(...)` builder is **not** exposed in sql-mode. It is a Python DSL and it should stay
one; a SQL user reaching for incremental joins writes a merge over a join and gets the comprehensive
path, which is correct if not optimal. Say that plainly in the docs rather than hiding it.

### 2.5 Touch points

- `routes/deploy._pond_config:84` reads `[pond] sql` alongside `dbt_project`.
- `routes/deploy._discover_ripples:124` gains a third branch.
- `duck/executor.load_topology:23` gains the matching branch (it already has one for dbt).
- `duck/executor._import_ripples` gains the synthesised-callable path.
- `cli/pond.py demo` — a `--sql` demo set, the same pipeline as the default one written as SQL files.
- Validation: `sql`, `dbt_project` and `@ripple` code are mutually exclusive; 422 at deploy.

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

## 4. Sequencing

1. **Run instrumentation** (§1). Independent, small, and it changes what the quickstart and demo pages
   say — so it should be decided early even if built later.
2. **MotherDuck groundwork + `md://` Spout** (§3.0, §3.2). Small, self-contained, and it makes the
   ecosystem positioning concrete rather than rhetorical.
3. **Native `.sql` Ripples** (§2). Changes the shape of the quickstart, so it wants to land before the
   Getting Started rewrite is finalised.
4. **MotherDuck Flock engine** (§3.4). The largest of the three recommended items and the one with the
   best ratio of capability to code, but it is the least urgent for the documentation rewrite.
5. Deferred: MD data plane (§3.3), MD as Source first-class (§3.1), MD compute (§3.5), whole-terminal
   dispatch (§3.4).

## 5. Open questions

- **Instrumentation: how much counting is free enough?** The proposal is metadata-only reads and a hard
  rule against adding a scan, with unknowns reported as unknown. Is a `--stats` opt-in worth having for
  the expensive counts, or does an unknown-shaped hole in the summary undermine the demo?
- **`.sql` mode and sqlglot.** Dependency inference wants a real parser. Is a hard requirement on the
  `duckstring[lineage]` extra acceptable for sql-mode, or should the explicit `-- depends:` form be the
  only supported mechanism in v1?
- **Does sql-mode make dbt-mode redundant?** No — dbt brings tests, docs, macros and an existing
  investment. But the two overlap enough that the docs need a clear sentence on when to use which, and
  it is worth deciding that sentence before building.
- **MotherDuck destination URI shape.** `md://database/schema/table` is the obvious form, but MD's own
  addressing conventions should win over ours where they differ.
- **Does the MD Flock engine become the default** where MD is configured? Athena is the current default
  and it is strictly more restricted. Defaulting to MD when both are available seems right, but it is a
  behaviour change worth stating rather than sliding in.
