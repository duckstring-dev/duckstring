# Data plane: clustering a merge table's cold base

Status: **built (2026-10-04).** Designed with the author on 2026-10-04, replacing a June proposal that
paired the ordering with Parquet bloom filters and external pk indexes (dropped: blooms are DuckDB's own
business, and native min/max pruning needs no index once the data is ordered well).

## Problem

A merge Trickle's older rows live in a compacted cold base, `{main}__base/` Parquet chunks, rewritten only
at a checkpoint. Reads that filter on a column (a query, the builder's affected-key filter on a joined
dimension, a lookup) prune row groups by their min/max statistics, which only works when similar values
are stored together. The base used to be written ordered by `_duckstring_f`, which helps nothing: every
base row is at or below `f_base`, so a freshness filter reads all of it or none. Measured on DuckDB 1.5 and
2.0 (`plans/duckdb-2-ready.md`), layout, not the DuckDB version, decides how much of a file a key-filtered
read fetches.

## Design (agreed)

- **Default: primary-key order.** With no declaration the base is sorted by `pk`, so pk filters (lookups,
  and the builder's key filter on a dimension joined by its key) skip almost everything. Fixed, not adaptive.
- **Opt in on the write**: `cluster_by` on `merge_table`, `apply_zset`, the builder's `.merge()`, and SQL
  Ripples (`cluster_by` in `[ripples.NAME]`, only with `write = "merge"`). A keyword on the write rather
  than a chained method, because it is storage, like `pk` and `compact_threshold`, not a transformation.
  Named `cluster_by`, not `zorder_by`: it also covers plain sorts, and `CLUSTER BY` is where Databricks
  (Liquid Clustering), Snowflake and BigQuery have settled.
- **One column**: a plain sort. **Two or more**: interleaved by default (`interleave=True`), as Databricks
  does; `interleave=False` sorts by them in order instead.
- **Interleaving is a rank-Morton order** (a copula Z-order): each column is replaced by its quantile rank,
  flattening its distribution to uniform, and the ranks' bits are interleaved, one per column per round,
  most significant first, in list order. Skewed columns get as much ordering as even ones.
- **`cluster_bits`**: the key's total bits; the data splits into `2**cluster_bits` cells. Chosen
  automatically at each compaction when unset: `ceil(log2(rows / row_group_rows)) + 1`, clamped to
  `[columns, 63]`, so a cell is a little under one row group (finer can't help min/max pruning, and more
  bits cost more quantile buckets). Split equally across columns, any remainder to the first columns, so
  list order is priority. No per-column bits: they added little.
- **No bloom filters, no external index.**
- **Declared per write**: omitting `cluster_by` clears it (back to pk order at the next compaction).
  Applies when the base is next rewritten; never touches the version contract.

## As built

- `trickle/io.py`: `cluster_spec(cluster_by, interleave, cluster_bits)` validates (unknown or repeated
  columns, `cluster_bits` without interleaving or out of `[columns, 63]`) and returns
  `{"by", "interleave", "bits"}`; `_set_cluster` stores it as JSON in a new `cluster` column of the
  `_duckstring_trickle` meta table (added by `_ensure_meta`'s migration path), checking the columns exist.
  `apply_zset`/`merge_table` validate before writing and record after; the builder's `.merge()` validates
  up front and records after whichever write path ran (aggregate, accumulate, comprehensive, incremental).
  Capture's `.merge()` accepts and ignores the arguments (ordering changes no column's derivation).
- `dataplane.py`: `_publish_base_chunks` writes `_base_order_select` (pk order, a sort, or the rank-Morton
  key) with `ROW_GROUP_SIZE 122880` set explicitly (`_ROW_GROUP_ROWS`, which the automatic bits size
  against). `rank_morton_sql` computes **exact** ranks with `NTILE` window functions rather than the
  sampled quantiles the June proposal described: no sampling error, one sort per column at compaction,
  which only runs when the base is rewritten anyway.
- **NULLs**: a column that has NULLs keeps its top bucket for them and ranks its other values in the rest;
  one without NULLs uses every bucket. Reserving the bucket unconditionally (the June proposal) halved a
  2-bit column's resolution and left a 1-bit column unclustered; found by the tests.
- Not stored in the sidecar: only the writer needs it, and every merge write re-declares it, so a
  rebuilt registry has it again before its next checkpoint.

## Measured

1M rows, two columns of 1,000 evenly spread values, 10,000-row row groups (98 of them), share of row groups
a filter on one value touches:

| Order | First column | Second column |
|---|---|---|
| Primary key | 100% | 100% |
| Sorted by both (`interleave=False`) | 1.1% | 100% |
| Rank-Morton, two columns | 14.8% | 19.8% |
| Rank-Morton, three columns (with `id`) | 27% | 32% |

Close to the ideal `1 / groups^(1/columns)` (10% for two, 22% for three); the gap is the Z-order's jumps at
quadrant boundaries. With few row groups (under about 20) interleaving gains little, which the guide notes
as a reason to keep the column list short. Tests: `tests/test_clustering.py`.

## Deferred

- Clustering overwrite tables (`write_table`): it would sort the whole table on every run, so only on request.
- A Hilbert curve instead of Z-order: better locality, but awkward in SQL; the author prefers rank-Morton.
