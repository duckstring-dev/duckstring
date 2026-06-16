# Data plane: Iceberg base layer + version-contract enforcement

Status: **partially implemented (2026-06-16)**. Covers **Phase 1** (swap the Parquet export/read for an
Iceberg table layer) and **Phase 2** (enforce the version contract at Source→Sink boundaries).
**Trickle** (append + `_duckstring_f` + merge + windowed incremental + incremental draws) is deferred to
its own session; this plan only lays the seams Trickle needs and calls them out explicitly under
**Trickle-prep**.

**Shipped this session (zero-dependency seams):**
- The pluggable **data-plane interface** (`src/duckstring/dataplane.py`): `ParquetDataPlane` as the
  zero-dep default behind `get_data_plane()` (env `DUCKSTRING_DATA_PLANE`); a write **`mode`**
  (`overwrite` now; `append`/`merge` reserved → raise) and a per-run **`f`** stamp threaded through
  every call site; the **`_duckstring_*` namespace reserved** (rejected at publish). The executor
  export, local-runner export+seed, `Pond.read_table` foreign reads, and `/api/data` all route through
  it. The draw/duct raw-Parquet transfer (poller.py/draw.py) is intentionally left untouched.
- **`pond.previous_f`** end-to-end (driver job → `DuckCore` → executor → `Pond`; local runner via a
  `puddles/.run_f` marker), documented in python-api.md + the Incremental Ripples guide.
- **`min_version` enforced at deploy** (`routes/deploy.py`): sink-under-pin + source-downgrade guards,
  with a major-bump escape hatch.

**Deferred (the dependency-adding, spike-laden part):** the actual **Iceberg backend** (pyiceberg +
SqlCatalog/SQLAlchemy, snapshots stamped with `f`, as-of read), and **Phase 2 schema capture +
schema-compatibility enforcement** (`pond_version_schema`, blocked-by-contract surfacing). The seams
above are shaped so these slot in without touching call sites.

## Why

Two gaps from the v0.2.0 review remain Duckstring's own responsibility:
1. **The version contract isn't enforced.** `min_version` is stored-but-unenforced (`pond_to_pond`),
   and nothing checks that a Sink's pinned Source major still presents a compatible schema. The whole
   value prop — versioned boundaries so breaking changes don't need org-wide coordination — is
   advisory until this is enforced.
2. **No substrate for incremental.** Whole-table Parquet replace (`duck/executor.py:_export_parquet`,
   `core.Pond.write_table`) gives no snapshots, no schema metadata, no path to incremental draws.

Adopting a **table format** (Apache Iceberg over the Parquet files we already write) services both: it
hands us schema metadata for (2)→contracts, and snapshots for the later Trickle work. Iceberg is a
metadata/snapshot + catalog layer; **the data files stay Parquet**. This is not a file-format swap.

## Settled design decisions (from the design discussion)

These constrain the design even though most only pay off in Trickle. Recorded so Phase 1/2 don't paint
into a corner.

- **Ripple = overwrite; Trickle = history-preserving append.** Binary, no middle option: "append then
  always compact" collapses to overwrite (same bytes, same cost), and append-for-write-perf solves a
  non-problem at the single-node ~50M-row scale (compute is the bottleneck, not the write). So a Ripple
  always writes the full current state; only a Trickle preserves per-run history.
- **The freshness stamp lives in the *data*, not in snapshot cursors.** Incremental consumption will
  be a content predicate (`WHERE _duckstring_f …`), not a snapshot-diff. This (a) sidesteps pyiceberg's
  immature incremental snapshot-scan API, (b) works on plain Parquet too, and (c) **decouples
  compaction from consumers** — rows keep their `_duckstring_f` through `rewrite_data_files`, so
  compaction can never break a lagging consumer. It's a **Trickle** concern (a Ripple writing overwrite
  needs no stamp); see Trickle-prep.
- **System columns use the reserved `_duckstring_*` namespace.** The freshness stamp is
  **`_duckstring_f`** — `f` matches the public API (`pond.f` / `pond.previous_f`), and the
  multi-character vendor prefix reads as "framework-owned, persisted" (distinct from a bare `_x`
  transient/scratch column), with room for siblings later (`_duckstring_op` for merge, etc.). Precedent:
  Airbyte `_airbyte_*`, Iceberg `_file`/`_pos`. The **whole prefix** is reserved, not a single name.
- **Incremental read is a window `(previous_f, f]`, not `_duckstring_f > previous_f`.** The upper bound (the run's
  own freshness `f`) is the **exactly-once boundary**: it stops a consumer re-reading rows from a
  source that independently ran ahead of the consumer's coordination epoch. Both bounds come from the
  consumer's own freshness — **no per-edge watermark**. Windowing is *definitional* to a delta read, so
  it correctly applies only to Trickle; full reads (Ripple) re-read everything and can't over-read.
- **Incremental chains Trickle→Trickle only.** A windowed read needs the source to preserve `_duckstring_f`
  history. A Trickle reading a Ripple (overwrite) source falls back to a full read; a draw from a
  Ripple is therefore necessarily get-all (so **Phase 1 does not change draw behaviour**).
- **The "up to this `f`" read semantic is contained to delta reads.** A full read still returns
  most-recent-possible. So Phase 1/2 introduce no freshness-semantic shift; that arrives with Trickle.

## Phase 1 — Iceberg base data plane

Behaviour-neutral: overwrite-per-run, draws stay get-all. The win is snapshots + schema metadata.

### Dependency & catalog
- Add **`pyiceberg`** (+ `pyarrow`, already implied). Catalog = pyiceberg **`SqlCatalog`** backed by
  SQLite. Note: `SqlCatalog` pulls **SQLAlchemy** — a non-trivial new dep; gate it behind a
  `duckstring[iceberg]` extra and keep the data plane **pluggable** (a thin interface with the current
  Parquet-replace as the zero-dep default, Iceberg as the recommended/default-on backend).
- **Do not** reuse the per-line `registry.duckdb`. One catalog DB, **namespace per `name@major`**, to
  preserve the major-line isolation that `ponds/{name}/m{major}/` already gives. Catalog DB lives at
  the catchment root (its own file; not `duck.db` — keep the orchestration DB and the data catalog
  separate). Local/puddle runs use a filesystem-rooted catalog under `puddles/`.

### Write path (`core.Pond.write_table` + `duck/executor.py:_export_parquet`)
- Replace the COPY-to-`{table}.parquet` export with an Iceberg **overwrite** commit: materialise the
  ripple's result to Arrow, `table.overwrite(arrow)` (create table on first write, inferring schema).
- **One snapshot per Pond Run, stamped with `f`.** Record the run's freshness on the snapshot
  (snapshot summary property, e.g. `duckstring.f = <iso>`) so a snapshot is resolvable from a freshness.
  This is pure Phase-1 plumbing but it's the hook Trickle's as-of/windowed read keys on.
- Keep the write idempotent across crash-replay/immediate-retry at the same `f` (overwrite already is —
  it replaces table state; re-running at the same `f` yields the same state).

### Read path (`core.Pond.read_table` + `/api/data` `routes/data.py`)
- Own tables: read from the registry as today (unchanged — ripples still compute on the DuckDB
  registry; Iceberg is the *export/interchange* layer, not the compute engine).
- Source tables: read the Source's Iceberg table instead of globbing `{table}.parquet`. Register it as
  a temp view named after the table (preserve the "no replacement scans" contract — see CLAUDE.md).
  Read via DuckDB's `iceberg` extension (read-side) **or** a pyiceberg scan → Arrow → register; pick
  one in the spike based on the extension's snapshot-selection ergonomics.
- **As-of read by `f`.** `read_table` resolves "the Source snapshot whose `f <= my f`" (replay-stable,
  and the lower half of Trickle's window). Phase 1 default stays "latest snapshot" for full reads;
  expose the as-of selection as the seam, don't change the default.
- `/api/data` reads the current snapshot via an in-memory DuckDB (as today, but Iceberg-aware), never
  the live registry.

### Draw / duct (`routes/draw.py`, `poller.py`)
- **Unchanged behaviour: get-all.** The draw still ships the current table's files. Optionally wire the
  *protocol slot* now (consumer sends its `end_f`); it's a no-op against overwrite tables and "wakes
  up" when Trickle lands. Low priority — fine to defer to the Trickle session.

### Migration / back-compat
- Migration `005_*.sql` if any catalog-pointer bookkeeping is needed in `duck.db`; the Iceberg catalog
  itself is a separate DB it manages.
- Transitional `read_table`: fall back to the legacy `{table}.parquet` if a Source has no Iceberg table
  yet (a Source deployed pre-upgrade that hasn't re-run). Remove the fallback once all Ponds re-export.
- `duckstring catchment download/archive` (`routes/catchment.py`) must include the catalog DB +
  Iceberg metadata/data dirs in the tar (snapshot the catalog SQLite via the backup API like `duck.db`).

### Trickle-prep in Phase 1
- Snapshot-per-run stamped with `f` (above) — the resolver Trickle's window needs.
- Keep the data-plane interface’s `write` able to express modes later (`overwrite` now; `append`,
  `merge` reserved). Don't bake "overwrite" into call sites — route through the interface.
- Reserve the **`_duckstring_*` namespace** now: reject any user output column whose name starts with
  `_duckstring_` at write, with a clear error. This protects `_duckstring_f` (and future system columns
  like `_duckstring_op`) before they exist, so Trickle can own them without a later breaking rename.

## Phase 2 — Version-contract enforcement at the boundary

Small increment on Phase 1's schema metadata; this is the strategic differentiator.

### Capture
- On deploy, capture each Pond's **output schema(s)** (Arrow/Iceberg schema per table) against the
  `pond_version` (immutable artifact). New table `pond_version_schema(pond_version_id, table, schema_json)`
  (migration `006_*.sql`), or store on the Iceberg table and read back — decide in the spike (storing in
  `duck.db` keyed on `pond_version` matches the "history/topology keyed on pond_version" convention).
- A Pond can't always declare its schema pre-run (it's the ripple output). Capture on **first
  successful run** of a `pond_version` and freeze it; a later run whose schema differs is a contract
  violation against *itself* (surface as a Pond failure with a clear message).

### Enforce
- **`min_version`** (`pond_to_pond.min_version`, currently stored-but-unenforced): at deploy and at
  resolve time, reject/block a Sink whose pinned Source **selected** version `< min_version` within
  the pinned major. Wire into `Driver.resolve` / deploy guard (`routes/deploy.py`).
- **Schema compatibility** at the Source→Sink edge: when a Source publishes a new selected version on a
  major line, check the consuming Sinks' recorded expectations against the new schema. Compatibility =
  the Sink's required columns/types still present (additive changes OK; drops/renames/type-narrowing on
  a *same-major* line are violations — a major bump is the sanctioned escape hatch). On violation:
  block the Sink (reuse the existing `is_blocked` machinery + `blocked_by`) with a contract message,
  rather than letting it run against an incompatible Source.
- Where the Sink's "expectations" come from in Phase 2: the columns it actually reads. Minimal viable:
  the Source's frozen output schema *is* the contract, and the check is "did a same-major redeploy break
  it." A declared per-Sink required-column set is a later refinement (and a natural Trickle-era addition
  alongside PK declaration).

### Surfacing
- New blocked sub-reason (contract) in `/api/status` `blocked_by` + a Sidebar StatusBox entry (mirror
  the Missing-Sources / Upstream-unavailable treatment). Run history / the failed Pond carries the
  contract message.

### Trickle-prep in Phase 2
- The schema-capture table is the natural home for **primary-key declaration** (Trickle requires
  declared PKs per table at write). Shape `pond_version_schema` so a `primary_key` column can be added
  without restructuring.
- Contract checks for Trickle sources will additionally compare PK declarations across versions
  (changing a PK on a same-major line is a violation). Leave the comparison pluggable.

## `pond.previous_f` exposure (both phases)

Expose the **previous run's freshness** to ripple code, so a user can hand-roll incremental logic
today (read `(pond.previous_f, pond.f]` from a Source themselves) without waiting for Trickle — and so
the windowed read has a single obvious source of truth when Trickle formalises it.

- **Value**: the Pond's `end_f` *before* this run advanced it — i.e. the last successfully completed
  run's freshness, `NEVER` on first run. Available at dispatch (`Driver._dispatch_begin_run`) as the
  engine pond state's `end_f` *before* the run.
- **Plumbing**:
  - `Driver._dispatch_begin_run`: add `previous_f` to the `begin_run` job (`driver.py:918`), read from
    `self.state.pond_states[pond].end_f` at dispatch.
  - Duck: carry it through `begin_run` → `RippleExecutor.submit` → `_run_ripple` →
    `Pond(previous_f=…)` (`duck/executor.py`, `engine/worker.py` job shape, `duck/core.py`).
  - `core.Pond.__init__`: store `self.previous_f` next to `self.f`; default `None`/`NEVER`.
  - Local runner (`local/runner.py:137`): `previous_f` = the prior local run's `f` if a self-puddle
    seed exists, else `NEVER` (mirrors the self-puddle incremental-rerun idempotency already in place).
- **Docs**: document alongside `pond.f` in python-api.md + the Incremental Ripples guide, framed as
  "the bracket `(previous_f, f]` for hand-rolled incremental; Trickle will automate this." Note the
  exactly-once ceiling caveat (read *up to* `f`, not the Source's latest) so hand-rollers don't
  reintroduce the over-read.

## Deferred to the Trickle session (do **not** build here)

- `append_table` (history-preserving, `_duckstring_f`-stamped, idempotent-on-`f` via
  delete-where-`_duckstring_f`+insert).
- The windowed `(previous_f, f]` delta read + the "up to `f`" as-of upper bound as a first-class read.
- Merge/upsert (declared PK, Iceberg merge-on-read / delete files) and PK-aware consumption.
- Incremental draws (ship files where `max(_duckstring_f) > consumer end_f`) + the "keep recent K runs
  uncompacted" transfer optimisation.
- Compaction/expiry scheduling (now safe to run on the producer's own clock — consumers are
  content-addressed by `_duckstring_f`).
- The "up to this `f`" freshness-semantic decision (mild shift from most-recent-possible) — confirm
  deliberately before building.

## Open questions for the spike

- DuckDB `iceberg` extension read ergonomics for snapshot/as-of selection vs. pyiceberg scan→Arrow.
- Catalog file placement + inclusion in `catchment archive`; concurrency vs. the Duck's registry writes.
- Exact dependency footprint of `pyiceberg[sql]` (SQLAlchemy) and whether to vendor a lighter catalog.
- Schema storage: `duck.db` (`pond_version`-keyed) vs. reading back from the Iceberg table.

## Testing

- Phase 1: round-trip write/read via Iceberg equals the prior Parquet behaviour (existing
  `test_runtime` e2e on the demo ponds must stay green); snapshot-per-run stamped with `f`; as-of
  resolver returns the right snapshot; `catchment archive` round-trips the catalog.
- Phase 2: `min_version` blocks an under-pinned Sink; a same-major schema-breaking redeploy blocks
  consumers with a contract message; an additive change does not; major bump is the escape hatch.
- `pond.previous_f`: correct value across first run / steady state / crash-replay (stable) / force.
- `ruff check .` clean; frontend `tsc`/eslint clean for the new blocked sub-reason.
