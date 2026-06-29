# Data plane: DuckLake as a third pluggable backend

Status: **proposed (2026-06-28)**, not implemented. Adds **DuckLake** as a third
`DUCKSTRING_DATA_PLANE` backend alongside `iceberg` (the default) and `parquet`. **Additive, not a
replacement** — Iceberg stays the default and nothing is built *exclusively* for DuckLake. The seam
already exists (`src/duckstring/dataplane.py:DataPlane` + `get_data_plane()`); this plan slots a
`DuckLakeDataPlane` into it the same way `IcebergDataPlane` slots in today.

## Why

DuckLake is the format Duckstring half-built by hand. `iceberg_catalog.py` exists for exactly one
reason, stated in its own docstring: pyiceberg's only embedded catalog (`SqlCatalog`) drags in
SQLAlchemy *purely* to store one pointer row per table (`namespace.table → metadata.json`), so we wrote
a 200-line `FileCatalog` to replace just that pointer store with a JSON file. DuckLake's whole thesis is
**put the lakehouse metadata — snapshots, schema, file lists, stats — in a SQL database**, with data
still as Parquet, and its reference implementation is a DuckDB extension. Duckstring is DuckDB-native
everywhere and already has the extension-load machinery (`prepare()` does `LOAD iceberg`). So DuckLake
is the metadata-in-SQL idea done properly by the DuckDB team — the FileCatalog workaround's destination.

What it buys over Iceberg, concretely against the current code:
- **No pyiceberg, no SQLAlchemy, no `FileCatalog`.** The DuckLake plane needs only the `ducklake`
  DuckDB extension and a SQL metadata store — both already in the DuckDB orbit. (We do **not** delete
  `iceberg_plane.py`/`iceberg_catalog.py` — Iceberg stays the default — but the DuckLake plane carries
  none of that dependency weight.)
- **Atomic multi-table snapshot per Pond Run.** Today `IcebergDataPlane._commit` is per-table — each
  overwrite table is its own Iceberg commit. DuckLake does ACID multi-table transactions, so a whole
  Pond Run's overwrite set commits as **one** snapshot — a genuinely better fit for the existing
  one-commit-per-run model.
- **Metadata is SQL rows, not a pile of files.** Most of `_prune` + `_gc_orphan_files` (the manual
  snapshot-expire + orphan-Parquet/manifest GC that exists only because pyiceberg 0.11 expires metadata
  but leaks the files) has no analogue to maintain — DuckLake tracks files as rows and has its own
  expiry/compaction. Snapshot/as-of resolution (`_snapshot_for`'s scan + the two-snapshots-per-overwrite
  tie-break wart) becomes a `WHERE` on a snapshot table.

### Why additive, not a swap

The decision (author, this session): **external Iceberg-ecosystem interop is the priority right now.**
Iceberg's payoff is precisely that Spark/Trino/Snowflake/etc. can read the published tables; DuckLake's
ecosystem is younger and DuckDB-centric (open spec, but 0.x and fast-moving). So Iceberg remains the
default plane, and we will not build for DuckLake exclusively. DuckLake earns its place as the
**DuckDB-native, lighter-dependency** option for deployments whose published tables are read back by
Duckstring/DuckDB rather than an external lake. The pluggable seam (`parquet` already proves the
fallback pattern) is exactly the mechanism for "another option, not the One True Way."

## The seam it slots into (no call-site changes)

The `DataPlane` contract is small and already plane-agnostic where it matters:
- `export(con, data_dir, *, mode, f)` — publish the registry's tables.
- `prepare(con)` — make a connection able to read this backend (load the extension).
- `_raw_read_select(data_dir, table, *, as_of)` — a physical `SELECT` over one published table.
- `list_tables(data_dir)` / `table_path(data_dir, table)`.
- **Merge-main reconstruction lives in the base class** (`DataPlane.read_select` /
  `_reconstruct_select` / `consolidated_count_select`) over each plane's `_raw_read_select`, so a new
  backend implements only the raw read and inherits Trickle reconstruction unchanged.

`get_data_plane()` gains a `ducklake` branch; every call site (executor export, local-runner
export+seed, `Pond.read_table` foreign reads, `/api/data`) already routes through the interface, so none
of them change.

## Settled design decisions

These mirror the Iceberg plane's hard-won choices; deviations are called out.

- **`DuckLakeDataPlane` is `IcebergDataPlane`-shaped: catalog for overwrite reads + a flat
  `ParquetDataPlane` sidecar for everything else.** The plane holds a `self._parquet = ParquetDataPlane()`
  and writes the flat sidecar *first* on every `export` (behaviour-neutral, and the fallback if the
  DuckLake commit fails), exactly as `IcebergDataPlane.export` does. Only **plain overwrite** tables are
  committed to DuckLake.
- **Keep the flat sidecar even though DuckLake *could* hold the append-only tables.** The append-only
  carve-out (append histories, `__changelog`, `__droplog`, and the merge **base** are served from the
  flat per-run parts layer, not the catalog) stays — but for a *different* reason than Iceberg's. With
  Iceberg the reason is O(runs) metadata-file growth; DuckLake tracks files as rows and inlines small
  changes, so that specific objection weakens. The reason the flat layer stays is the **cross-Catchment
  draw**: `routes/draw.py` + `poller._land_transfer` ship raw Parquet parts by filename and the consumer
  drops them in idempotently — a self-contained, format-free transfer that rides the flat layer, plus
  `/api/data` direct file-serve. DuckLake owns its data-file layout, so we can't ship its files without
  shipping its metadata rows. So `_raw_read_select` falls back to `self._parquet._raw_read_select` for
  any table not committed to DuckLake — identical to `IcebergDataPlane._raw_read_select`'s fallback.
  **Phase 1 does not touch the draw path.**
- **One metadata store per `name@major` line**, at `{data_dir}/catalog.ducklake` (or `.sqlite`), data
  files under `data_dir`. Preserves the physical major-line isolation `ponds/{name}/m{major}/` already
  gives and the single-writer-per-line invariant (so, as with `FileCatalog`, no optimistic-concurrency
  concern). This deliberately matches the per-line-catalog deviation the Iceberg plane already settled
  on, rather than DuckLake's natural "one shared metadata DB" — keep isolation physical.
- **SQLite-backed metadata, not DuckDB-backed** (spike-confirmable). DuckLake can keep its catalog in
  DuckDB, SQLite, Postgres, or MySQL. SQLite keeps `catchment archive`/`download` trivial: the metadata
  is a plain file the existing root walk copies, snapshotted via the backup API like `duck.db` (a
  DuckDB-backed catalog would need the snapshot-while-quiescent treatment the registries get). Postgres/
  MySQL are out of scope (they reintroduce an external service Duckstring's single-Catchment positioning
  avoids).
- **One snapshot per Pond Run, stamped with `f`.** The whole overwrite set commits in one DuckLake
  transaction; the run's freshness is recorded against the snapshot (a snapshot property / tag, or a
  small `f → snapshot_id` mapping we maintain — see open questions). This is the hook the as-of read
  keys on, same role as Iceberg's `duckstring.f` snapshot summary property.
- **As-of read by `f` via DuckLake time-travel.** `_raw_read_select(..., as_of=)` resolves the snapshot
  whose stamped `f <= as_of` and reads at it (DuckLake `AT (VERSION => …)` / `AT (TIMESTAMP => …)`);
  `as_of=None` reads latest. Same seam, same default-latest behaviour as Phase-1 Iceberg.
- **`_duckstring_f` exported as UTC.** Carry over the Iceberg plane's `SET TimeZone='UTC'` discipline
  for the timestamp column (defensive even if DuckLake is less strict than pyiceberg about tz).

## Phase 1 — the DuckLake backend

New module `src/duckstring/ducklake_plane.py`: `class DuckLakeDataPlane(DataPlane)`, structured as a
close cousin of `IcebergDataPlane`.

### Dependency & selection
- No new Python deps — just the `ducklake` DuckDB extension (downloaded once on first `prepare`, like
  `iceberg`). `get_data_plane()` (`dataplane.py`) gains:
  ```
  if backend == "ducklake":
      from .ducklake_plane import DuckLakeDataPlane
      return DuckLakeDataPlane()
  ```
  Update the `ValueError` message to list all three backends and the docstring's backend table.

### prepare(con)
- `INSTALL ducklake; LOAD ducklake` (mirror `IcebergDataPlane.prepare`'s try/except install-then-load).
- The ATTACH of the per-line metadata store happens at the read/write sites (it needs `data_dir`), not
  in `prepare(con)` which only has the connection — same split the Iceberg plane uses (`prepare` loads
  the extension; `_catalog(data_dir)` opens the per-line catalog).

### export(con, data_dir, *, mode, f)
1. `_check_mode(mode)`, then `publish_plan(con, data_dir, f)` (validates the publish set — Trickle tables
   exempt — and writes the `_trickle.json` sidecar). Identical preamble to the Iceberg plane.
2. **Flat sidecar first**: `self._parquet.export(con, data_dir, mode=mode, f=f)` — keeps draw/direct-
   serve/append-only working and is the consistent fallback.
3. `SET TimeZone='UTC'`.
4. ATTACH the line's DuckLake store (`ATTACH 'ducklake:{catalog.ducklake}' AS lake (DATA_PATH '{data_dir}')`
   — exact DDL per the spike), `BEGIN`, and for each table that is **plain overwrite** (skip
   `_is_incremental(table, meta)` and `meta[table]['mode'] == 'merge'`, exactly as the Iceberg loop
   does) `CREATE OR REPLACE TABLE lake.{table} AS SELECT * FROM "{table}"`. Record `f` against the
   snapshot, `COMMIT`. One transaction = one snapshot for the whole run.
5. Schema drift is free here: `CREATE OR REPLACE` (or DuckLake schema evolution) handles a changed
   overwrite schema without the drop-and-recreate dance `_commit` needs for pyiceberg.

### _raw_read_select(data_dir, table, *, as_of)
- If the table isn't in the DuckLake catalog (a merge base/append-only served from the flat layer, or a
  Source not yet re-exported) → `return self._parquet._raw_read_select(data_dir, table, as_of=as_of)`.
- Else read the attached table, at the as-of snapshot when `as_of` is given:
  `SELECT * FROM lake.{table}` / `... AT (VERSION => {snap})`. The base class's `read_select` /
  `_reconstruct_select` handle merge reconstruction over this unchanged.

### list_tables / table_path
- Both delegate to `self._parquet` (the flat sidecar is written for every published table, so its
  listing is the authoritative publish set) — identical to the Iceberg plane.

### Archive / download
- A SQLite-backed `catalog.ducklake` and the DuckLake data files live under `data_dir`, so
  `catchment archive`'s root walk picks them up with **no archive change** (snapshot the SQLite file via
  the backup API like `duck.db`/`pond.db`; download while quiescent, as today).

### Offline
- Like the Iceberg extension, `ducklake` is a one-time download; an offline Catchment uses
  `DUCKSTRING_DATA_PLANE=parquet`. Document alongside the existing offline note.

## Non-goals / explicitly deferred

- **Not the default, and no exclusive features.** Anything Duckstring offers must work on `iceberg` and
  `parquet` too. DuckLake-only capabilities (e.g. catalog-level partitioning hints) are out of scope.
- **No new Trickle path.** Append-only/merge tables stay on the flat parts layer; the draw protocol is
  unchanged. Folding the append-only tables into DuckLake (now metadata-cheap) is a possible *later*
  optimisation, but it would have to keep the flat layer for transfer anyway, so it earns little — left
  out deliberately.
- **No removal of the Iceberg plane.** (If a future review ever flips the default to DuckLake — only if
  external-reader interop stops mattering — `iceberg_plane.py` + `iceberg_catalog.py` + the pyiceberg
  dep become removable. That is **not** a goal of this work; recorded only so the option is visible.)

## Open questions for the spike

- Exact `ducklake` ATTACH DDL, the `DATA_PATH` semantics, and the time-travel read syntax
  (`AT (VERSION => …)` vs `AT (TIMESTAMP => …)`; `ducklake_snapshots()` for resolution).
- How to stamp/resolve the run's `f` on a snapshot: a native snapshot property/tag if the extension
  exposes one, else a tiny `f → snapshot` mapping table we maintain in the metadata store (mirrors the
  role of Iceberg's `duckstring.f` summary property and `_snapshot_for`).
- SQLite vs DuckDB metadata backing under concurrent cross-Pond *readers* while the line's single Duck
  writes (we expect fine — single writer per line — but confirm reader isolation during a commit).
- Whether `CREATE OR REPLACE` per run accumulates old data files that DuckLake's own expiry reclaims, or
  whether we need an explicit cleanup call (the analogue of `_prune`, hopefully much smaller).
- Minimum DuckDB version that ships a stable `ducklake` extension vs. the version Duckstring pins.

## Testing

- `tests/test_ducklake.py` mirroring `tests/test_iceberg.py`: round-trip write/read, `f`-stamp, as-of
  snapshot resolution, reserved-namespace rejection, flat-layer fallback for append-only/merge tables,
  overwrite-schema change, and `Pond.read_table` over a DuckLake Source.
- An e2e demo-chain variant (the analogue of `test_demo_chain_runs_on_iceberg_end_to_end`) on real Duck
  subprocesses with `DUCKSTRING_DATA_PLANE=ducklake`. `test_runtime`'s broad suite stays pinned to
  `parquet` (fast/offline) and the default plane stays `iceberg`, so the rest of the suite is unaffected.
- `catchment archive`/`download` round-trips a DuckLake line (SQLite metadata + data files).
- `ruff check .` clean.
