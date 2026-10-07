# Clustering benchmark

How the row order of a Parquet table affects its write cost, its size, how much of it DuckDB's min/max
statistics let a filtered read skip, and query time. The table is TPC-DS `store_sales`, the engine is DuckDB,
and the orders compared are:

| Layout | Order |
|---|---|
| `generated` | as `dsdgen` writes it (ticket order); a no-sort reference for write time and size |
| `hash` | by a hash of the primary key: effectively random |
| `lexicographic` | `ORDER BY c1, c2, ...` |
| `morton` | Z-order over each column scaled linearly between its min and max |
| `hilbert` | Hilbert curve over the same scaled columns |
| `rank_morton` | Z-order over each column's quantile rank, ranked with `NTILE` windows (Duckstring 0.6.0) |
| `rank_hilbert` | Hilbert curve over the same `NTILE` ranks |
| `qrank_morton`, `qrank_hilbert` | the same ranks from exact quantile boundaries (one `quantile_disc` per column), each row bucketed by one comparison per boundary |
| `arank_morton`, `arank_hilbert` | boundaries from `approx_quantile`, each row bucketed by binary search. `arank_hilbert` is what Duckstring's `cluster_by` does now (with exact boundaries below ten million rows) |

The interleaved layouts separate the ideas: ranking against scaling, Morton against Hilbert, and three ways
of computing the ranks. The SQL for each is in `encodings_sql.py`. `check.py` verifies the Hilbert SQL (a
bijection whose consecutive indices are grid neighbours), that binary-search bucketing matches the linear
version, and that `qrank_hilbert` produces exactly the key of `duckstring.dataplane.rank_hilbert_sql`.

## Column sets

- `keys`: `ss_sold_date_sk`, `ss_item_sk`, `ss_customer_sk`. Near-uniform surrogate keys, where scaling and
  ranking should behave alike.
- `skewed`: `ss_sold_date_sk`, `ss_net_paid`. A long-tailed amount (at SF1 the median is 865 against a max
  of 19,443), where linear scaling crowds most rows into a few buckets.

Every interleaved layout uses the same bits per column: Duckstring's automatic total (cells just under one
122,880-row row group) split evenly, rounded up, since the Hilbert code needs equal bits. `--bits` overrides.

## What is measured

Per layout:

- **Stats**: the column scan the interleaved layouts need first (NULL counts, plus min/max for scaling).
- **Key**: computing the sort key alone (`SELECT max(key)`), so the payload columns are not carried.
- **Write**: the whole `COPY (... ORDER BY key) TO parquet`, which reads the source, computes the key, sorts
  full rows and writes them. Compare with `generated` for the cost of ordering.
- **Size**: bytes of the written Parquet (ordering changes compression).

Per query (each family has several instances drawn from a seeded sample, identical across layouts):

- **Row groups read**: the share of rows in row groups whose min/max statistics overlap the predicate,
  computed from `parquet_metadata`. This is deterministic and machine-independent, and it is what DuckDB's
  zone-map pruning can skip. DuckDB may skip more for equality predicates using Parquet bloom filters, which
  this figure ignores.
- **Query time**: median of `--repeat` runs after `--warmup`, so the OS file cache is warm. This measures
  the CPU and decode work pruning saves, not cold-storage reads; on object storage the gap between layouts
  would be wider.
- **Matched**: the share of rows the predicate actually selects, the lower bound for any layout.

Query families: equality on each column, ranges on each column selecting 0.1%, 1% and 10%, boxes over all
the set's columns selecting 0.01%, 0.1% and 1% overall, and four TPC-DS-style joins where a filtered
dimension prunes the fact table through DuckDB's dynamic join filters (for these the row-group figure uses
the build side's key min and max, which is the bound DuckDB pushes into the scan).

## Running

```bash
uv run python bench/clustering/check.py
uv run python bench/clustering/run.py generate --sf 100
uv run python bench/clustering/run.py run --sf 100
```

`generate` runs `dsdgen` (single-threaded, about 9 s per scale factor on an M-series laptop) and exports
`store_sales`, `date_dim`, `item` and `customer` to Parquet under `--data` (default `bench/clustering/data/`,
git-ignored), then deletes the intermediate database unless `--keep-db`. `run` builds each layout in turn,
measures it and deletes it unless `--keep`, so peak disk is the source plus one layout. Results go to
`results/sf{N}-{timestamp}/` (or `--out`): `config.json`, `encode.jsonl`, `queries.jsonl` (every timing),
the workloads, and `summary.md`. `summarise <dir>` rebuilds the summary.

Rough sizes for `store_sales` as Parquet: SF10 about 1.4 GB, SF100 about 14 GB. `--threads` and
`--memory` set DuckDB's limits; sorts larger than memory spill to `--data/tmp`.

## Notes

- `NTILE` splits rows with tied values across a bucket boundary in whatever order the plan delivers them,
  so the rank-Morton key is not deterministic where values tie. It doesn't affect pruning (a tied value can
  straddle two adjacent buckets either way), but it is why `check.py` compares keys on distinct values.
- `NTILE` ranking computes one window sort per column over the full-width rows before the final sort. At
  SF100 it filled the 60 GiB spill cap (`BENCH_MAX_TEMP`) and is recorded as failed. Boundary-based ranking
  needs one aggregate per column instead.
- `approx_quantile` returns slightly different boundaries on each multi-threaded run, so `arank_*` layouts
  can differ slightly between runs. Their pruning matched the exact `qrank_*` layouts on every query.

## File-level clustering

`file_level.py` compares the global sort with Delta Lake's default of range-partitioning rows into files by
the key without sorting inside them, on the SF100 `skewed` set with the `arank_hilbert` key. It needs the
SF100 source data and writes `results/sf100/filelevel.json`.
