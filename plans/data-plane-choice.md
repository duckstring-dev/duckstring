# Data plane: drop Iceberg, make versioned Parquet the default

Status: **decided (2026-10-02), not built.** The author chose C: remove the Iceberg plane entirely, with
no option to keep it. The lakehouse Spout is deferred indefinitely: nothing needs it now. Supersedes the
"Iceberg stays the default" position of `plans/data-plane-iceberg.md` and `plans/data-plane-ducklake.md`.
Depends on `plans/versioned-overwrite.md`, which is built first.

## Summary

The Iceberg plane costs about 3x the export time, 2.5x the memory and 3.5x the disk of the Parquet plane
for a large overwrite table, and the reasons it was adopted have since been met without it. Its one
remaining job, as-of reads of overwrite tables, is what `plans/versioned-overwrite.md` gives the Parquet
plane. Recommendation: build versioned overwrite, make the Parquet plane the default, and remove the
Iceberg plane and its dependencies. Meet external-engine interop at the edge, with a Spout that writes to
the user's own catalog, rather than in Duckstring's internal storage. DuckLake is a good candidate for
that Spout and a poor fit as the internal plane.

## Why Iceberg was adopted, and where each reason stands

From `plans/data-plane-iceberg.md` ("Why"):

1. **Schema metadata for the version contract.** The contract is captured from the Pond's DuckDB
   registry (`schema_contract.extract_schema(con)`), not from Iceberg metadata, and works on either plane.
2. **Snapshots as a substrate for incremental processing.** Trickle settled on `_duckstring_f` as a
   content predicate over flat per-run parts, and deliberately keeps every Trickle table out of Iceberg
   (`IcebergDataPlane.export`: an append-only table's snapshot would reference every file ever written).

From `plans/data-plane-ducklake.md`, the later reason for keeping it as the default:

3. **External interop**: Spark, Trino and Snowflake reading the published tables. In the current code
   this mostly doesn't hold:
   - Only plain overwrite tables are in Iceberg. Merge and append Trickle tables, the incremental core,
     are served from the flat layer only.
   - The catalog is our own `FileCatalog`, a JSON pointer file that no external engine discovers. A reader
     would have to be pointed at a `metadata.json` path, which changes every commit.
   - Under local-first publish the Iceberg catalog is local-only and never persisted
     (`dataplane._PERSIST_SKIP`: its metadata embeds absolute local paths). On an S3 data root, a Pond whose
     Duck runs on the Catchment's machine has no Iceberg metadata in the bucket; only remotely run Ponds do.
     So the tables an external engine could find are an inconsistent subset of an inconsistent subset.
   - The Athena Flock engine stages its own Parquet; its docstring's "the cloud registers the published
     Iceberg plane instead" has nothing persisted to register.

## What it costs

Measured 2026-10-02: one overwrite table, 20M rows, 6 columns (250 MB in DuckDB), exported by each plane in
a fresh process (`DataPlane.export`, local disk, Apple M-series):

| | Export time | Peak memory | Disk after 4 runs |
|---|---|---|---|
| Parquet plane | 0.4-0.6 s | 0.97-1.4 GB | 237 MB |
| Iceberg plane | 1.6-1.8 s | 2.3-2.55 GB | 798 MB |

- **Two full writes per run.** `IcebergDataPlane.export` first writes the flat Parquet layer
  (`self._parquet.export`), then commits the table again to Iceberg.
- **The whole table in memory.** The commit fetches the table as Arrow
  (`con.execute('SELECT * FROM table').fetch_arrow_table()`, `iceberg_plane.py`) before pyiceberg writes
  it. Memory scales with table size: a table several times larger than this one exceeds a typical Duck's
  memory at publish time, after its Ripples have succeeded. This is a design flaw independent of the
  rest.
- **Retained snapshots** multiply the disk (`DUCKSTRING_ICEBERG_KEEP_SNAPSHOTS`, default 5).
- **Complexity and fragility it brings:** `iceberg_catalog.FileCatalog`, snapshot pruning and orphan GC,
  the catalog-pointer reasoning behind `data_lease.py`, migration skip lists (`driver._MIGRATE_SKIP`,
  `_PERSIST_SKIP`), the `iceberg_scan` plan-serialisation workaround in `routes/data.py`, the UTC-only
  timestamp and BIGINT-weight constraints, the `iceberg` extension download (offline Catchments must opt
  out), and pyiceberg as a core dependency. A pyiceberg minor release (0.12) broke every fresh install on
  2026-09-29 (`load_view`/`register_view` became abstract).

## Options

**A. Keep Iceberg, fix the memory.** Stream record batches into the commit instead of materialising the
table. Removes the memory problem but keeps the double write, the disk, the complexity, and an interop
story that doesn't reach external engines. Not recommended.

**B. DuckLake as the internal plane.** DuckLake 1.0 (April 2026, DuckDB 1.5.2+) keeps metadata in a SQL
database (DuckDB, SQLite or Postgres) and data as Parquet, with snapshots, time travel, multi-table
transactions, data inlining, and reader clients for Spark, Trino and DataFusion. It is the DuckDB-native
answer and the destination the FileCatalog workaround was heading for. As Duckstring's internal plane it
fits badly:
- **Catalog placement.** Each `name@major` line has a single writer, but on an S3 data root that writer
  may be a Fargate task, an EC2 box, a Pool machine or the Catchment, and readers are on other machines. A
  DuckDB or SQLite catalog file can't be written on S3, so writers on several machines need a shared
  Postgres catalog: an external service Duckstring's single-Catchment positioning avoids.
- **It duplicates what Duckstring already does.** The Catchment already knows each line's published
  freshness and decides when Sinks may read; versioned files give as-of reads; schema contracts come from
  the registry. What DuckLake adds internally is mostly a second source of truth.
- **The flat layer stays regardless.** Cross-Catchment Draws ship raw Parquet by file name, which DuckLake's
  own file layout doesn't allow (`plans/data-plane-ducklake.md` reached the same conclusion), so it would
  be a third write path, not a replacement.

**C. Versioned Parquet as the only plane (recommended).** `plans/versioned-overwrite.md` gives overwrite
tables as-of reads with one write per run. Everything the Iceberg plane serves today is already served by
the flat layer, which every plane writes and every reader falls back to, so switching changes no reader.

## Interop, done at the edge

External engines look for tables in the user's own catalog (Glue, a REST catalog, Unity, Snowflake,
DuckLake on Postgres), so that's where to put them. A Spout already delivers a Pond's output to S3 and
Postgres; a lakehouse Spout would write a chosen table into the user's catalog as Iceberg or DuckLake, with
the user's credentials and naming. That is opt-in per table, costs nothing for Ponds that don't use it,
and makes the published table visible where the external engine actually looks. DuckLake is a natural
first target: the `ducklake` extension writes it from DuckDB with no extra Python dependency, and its
catalog can be the user's Postgres. An Iceberg Spout would follow if users ask (pyiceberg as an optional
extra, writing to their REST or Glue catalog).

## Steps

1. Build `plans/versioned-overwrite.md` (about a day to a day and a half).
2. Make `parquet` the default `DUCKSTRING_DATA_PLANE`. Every published table already has its flat layer,
   so existing data reads unchanged.
3. Remove `iceberg_plane.py`, `iceberg_catalog.py`, the Iceberg snapshot and GC code, the `iceberg` branch
   of `get_data_plane`, `DUCKSTRING_ICEBERG_KEEP_SNAPSHOTS`, the pyiceberg dependency (pyarrow stays: the
   Flight server and other Arrow paths use it), `tests/test_iceberg.py` and
   `test_demo_chain_runs_on_iceberg_end_to_end`. Simplify `_MIGRATE_SKIP`/`_PERSIST_SKIP`, the
   `routes/data.py` serialisation workaround, and the comments that explain Iceberg constraints. Keep
   `data_lease.py`: two Catchments on one data root is still dangerous without a catalog pointer.
   About half a day.
4. Docs: `reference/environment.md` (the data-plane setting and its options; its current line "An
   object-store data root uses the Parquet data plane" is wrong today, since nothing forces it), the AWS
   guide's data section, and CLAUDE.md's data-plane section.
5. Later, if wanted: the lakehouse Spout (DuckLake first). Its own plan.

## Decisions (author, 2026-10-02)

- **Remove the Iceberg plane (C).** No `DUCKSTRING_DATA_PLANE=iceberg` option is kept: an option nobody
  can safely use at scale is a maintenance cost with no user. Steps 1 to 4 above.
- **The lakehouse Spout is deferred**, not part of this work or the next release; nothing needs it now.
  Step 5 stays recorded as the route if external-engine interop is ever asked for.

## Sources

- [DuckLake v1.0 announcement](https://ducklake.select/2026/04/13/ducklake-10/) (April 2026)
- [DuckLake 1.0 coverage, InfoQ](https://www.infoq.com/news/2026/05/ducklake-sql-catalog/)
