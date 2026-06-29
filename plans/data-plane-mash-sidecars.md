# Data plane: cold-tier clustering (MASH) + identity sidecars for merge mains

Status: **proposed (2026-06-28)**, not implemented. **Sequenced after `plans/data-plane-ducklake.md`**
and **builds directly on `plans/trickle-main-incremental.md`** (the merge-main LSM). This is a
read-acceleration layer on the merge-main **cold base** — it does not change the freshness/Z-set
semantics, the reconstruct correctness, or the per-run O(change) publish.

## What this is — and what already exists

The source design described a "two-tier storage with sidecar skip structures." **Duckstring already has
the two tiers** (three, actually — see `trickle-main-incremental.md`), so the only *new* ideas here are
the **MASH ordering of the cold base** and the **sidecar skip structures**. Mapping the source design's
vocabulary onto what's in the repo so nothing is rebuilt:

| Source design term            | Duckstring today (`trickle/io.py`, `dataplane.py`)                                  |
|-------------------------------|-------------------------------------------------------------------------------------|
| Cold tier (immutable segments)| L2 **clean cold base**, `{main}__base/` chunks, rewritten only at `checkpoint`       |
| Warm tier (small, Z-set)      | L1 warm bands `{main}__band/` (`fold_warm`) **+** L0 hot `__changelog` (per-run parts)|
| Query = cold ⊎ warm + DBSP    | `reconstruct_sql` — latest-per-PK over cold ⊎ warm ⊎ hot, cold anti-joined by retraction keys |
| Compaction (warm→cold)        | `checkpoint` (k=1: warm ≥ cold) → `_publish_base_chunks`                              |
| "segment"                     | a base **chunk** (`base_chunks`, `base_dir_name`) — same thing; this doc says *chunk* |

So: the cold base is **already** size-bounded chunks, but ordered by `_duckstring_f`
(`_publish_base_chunks` does `COPY (… ORDER BY _duckstring_f) … (FILE_SIZE_BYTES …)`), and there is
**no secondary index of any kind**. This plan changes the cold-base *ordering* to MASH and adds two
small sidecars, both **produced as a byproduct of the existing `checkpoint`/`_publish_base_chunks`
pass** — the one moment a full immutable cold partition is in hand and row ordinals are free to move.

### Scope: the merge-main cold base only

This applies **only to the merge-main `{main}__base/` chunks** — the single large, immutable,
compaction-produced segment set in the system. **Out of scope:** append-only Trickle tables and merge
`__changelog`/`__band` (immutable per-run parts, O(change)-small, never compacted to a base — nothing to
cluster or index at whole-segment granularity) and plain overwrite output (a single file / a
snapshot). If profiling later shows a warm tier large enough to want pruning, that's a separate,
evidence-driven follow-up.

### Where it lives across planes (important)

The merge-main base is **served from the flat `__base/` Parquet chunks on *every* plane** — the Iceberg
plane does not catalog-commit it (`iceberg_plane.py`: a merge base is "served from the flat layer", and
`_raw_read_select` falls back to `ParquetDataPlane`), and the DuckLake plan keeps the same carve-out
(the base rides the flat layer for the cross-Catchment draw). So **MASH + the sidecars are implemented
once, in the Parquet base-publish path** (`_publish_base_chunks` and a new companion writer), and all
three planes inherit them with no plane-specific work. This is why the doc is plane-agnostic despite
sequencing after DuckLake. (The DuckLake catalog *could* later serve the segment-elimination probe in a
single query — see Part C's caveat — but that is a deferred refinement, not the baseline.)

## Part A — MASH ordering of the cold base

### MASH, for the implementer

MASH (multi-column **a**pproximate **s**pace-filling **h**ash; a population-aware Morton/Z-order) is a
single physical row ordering over several columns that gives *approximate* clustering on all of them at
once, so range/equality predicates on any ordered column prune well via Parquet zone maps. The
population-awareness is the whole point — a raw Z-curve interleaves bits of the *raw* values and so only
balances when every column is uniform; MASH interleaves bits of the **quantile rank**, so **every `m`
bits of the MASH key isolates a `1/2^m` proportion of the data** regardless of distribution.

Computing a chunk's MASH key at `checkpoint`:
1. **Sample** ~10k rows from the base being rewritten.
2. For each declared order-key column, compute **approximate quantiles** from the sample
   (DuckDB `approx_quantile` / `quantile_cont`).
3. **Quantile-bucket to bits**: at a per-key bit depth `n`, map each value to an `n`-bit code by which
   quantile bucket it falls in (first `1/2^n` of the data → `0…0`, last → `1…1`). The bits encode
   *rank within the column's distribution*, not the raw value (`width_bucket(x, quantile_boundaries)`).
4. **Bit-interleave** the per-column codes (Morton-style) into one composite MASH integer.
5. Use that composite as **both** the row order **and** the chunk-partition order for the rewrite.

Quantile boundaries are computed per chunk-set from that compaction's sample, so they are recomputed at
each `checkpoint` — consistent with ordinals only being allowed to move at compaction. Bit depth `n` per
key is the one real tuning knob (ordering precision vs. key width); `k·n` should stay ≤ 64 so the
composite fits a `BIGINT` (a few keys at 8–10 bits each).

### Where it hooks

`_publish_base_chunks` (`dataplane.py`) currently does one `COPY (SELECT * FROM "{main}" ORDER BY
_duckstring_f) … (FILE_SIZE_BYTES …)`. The change: compute the MASH key (sample → quantiles → bucket →
interleave, in a CTE or a small Python-generated SQL expression / scalar UDF), then
`… ORDER BY mash_key` with `ROW_NUMBER() OVER (ORDER BY mash_key)` in the same pass to assign the
ordinals the sidecar records. **One ordering pass seals the chunk's clustering and produces the
pk→ordinal sidecar.** Chunk boundaries then fall along MASH order, so each chunk is a contiguous MASH
range and zone maps prune on the ordered predicate columns.

### Opt-in via declared order keys; default unchanged

MASH needs declared order-key columns, which Duckstring has no notion of today (a merge table declares
only `pk`). So:
- **New optional API**: order/cluster columns on the merge write — e.g. `merge_table(…, cluster_by=[…])`
  and `.merge(…, cluster_by=[…])`, recorded in the `_duckstring_trickle` meta + the `_trickle.json`
  sidecar (alongside `mode`/`pk`/`floor`/`f_base`).
- **Default = today's behaviour**: with no `cluster_by`, the cold base stays `_duckstring_f`-ordered and
  no MASH/zone-map clustering is built. This is the conservative, no-regression default and matches the
  "surgical, evidence-driven" philosophy — clustering is bought only where a table's read pattern asks
  for it.

**Decision point (the as-of / freshness trade-off):** the base is `_duckstring_f`-ordered today so the
as-of read (`_read_parquet_glob`'s `_duckstring_f <= as_of`) stat-prunes whole chunks. MASH-ordering by
predicate columns scatters `_duckstring_f`, so as-of-on-cold relaxes to a row-level filter (no
whole-chunk pruning). This is *probably fine* — the merge base is folded up to `f_base`, so an as-of
read below `f_base` is already unsupported (folding is lossy), and an as-of ≥ `f_base` reads the whole
base regardless. But it is a real behaviour change for a clustered table and must be confirmed before
building. Option if it matters: include `_duckstring_f` as one MASH key (partial freshness clustering +
predicate clustering). See Open questions.

## Part B — sidecar skip structures

Two small structures, written by the same `checkpoint` pass that rewrites the base, stored as **ordinary
flat-Parquet companion tables** next to `{main}__base/` (reserved-namespace names so publish/contract/
Iceberg treat them like the other system companions, e.g. `__changelog`/`__band`). Plane-agnostic.

1. **Per-chunk pk min/max** — `(chunk_id, pk_min, pk_max)` (one row per base chunk), optionally a coarse
   set summary / bloom. Given recency-clustered changes, cold chunks have largely disjoint pk ranges, so
   min/max alone is usually a strong **whole-chunk** filter; a hand-rolled bloom is only worth adding if
   pk ranges overlap heavily across chunks. (DuckDB already writes Parquet **row-group** bloom filters
   and prunes pk-equality on them at read; this sidecar is for *whole-chunk* elimination above that.)
2. **pk → chunk (→ ordinal) map** — a sorted `(pk, chunk_id, row_number)` table. Falls out for free from
   the `ROW_NUMBER() OVER (ORDER BY mash_key)` Part A already computes. The load-bearing field is
   `chunk_id` (which file holds a pk); `row_number` is recorded but its direct use is limited (Parquet
   has no cheap positional seek — within a chunk you rely on row-group blooms/zone maps), so treat the
   ordinal as secondary, not the mechanism.

### What they accelerate (be precise)

- **Point lookup by pk** (and any pk-filtered read): pk min/max eliminates whole chunks; the pk→chunk
  map names the surviving chunk(s); DuckDB's row-group blooms prune within. This is the global
  secondary-index-over-files that DuckDB natively lacks.
- **The reconstruct/consolidation anti-join** (`reconstruct_sql`): the cold base is anti-joined by the
  warm⊎hot **retraction keys**. The sidecar lets a chunk that **no** changelog pk touches skip the
  anti-join entirely (pass through clean). **Caveat the source design blurred:** for a *full* table
  read this avoids anti-join *work*, not data *read* — every cold chunk is still part of the current
  state and must be returned. The data-read win is only for *point/predicate* reads (where elimination
  drops whole chunks). Worth stating so the benefit isn't oversold.

The design bets on **recency-clustered changes** (edits cluster near create time → warm pks mostly
recent, cold mostly old → low warm↔cold pk overlap), which makes both wins land. It degrades under
scattered historical updates (backfills, GDPR sweeps) — the known boundary, not a bug.

## Part C — pulling it into queries (no engine control)

Pure SQL composition at the orchestration layer (the `read_select`/`reconstruct_sql` construction we
already own) — no DuckDB extension, no scan-operator hacks. DuckDB does the join, predicate pushdown,
and Parquet row-group pruning natively; the orchestration layer just hands it a narrower file list and a
pk filter. Construction:
1. Read the warm⊎hot pk set (already computed for the reconstruct window).
2. Probe the sidecar (chunk min/max, then pk→chunk map) → the minimal live-chunk list + pk filter.
3. Template the read: `read_parquet([live chunks only])` with the pk filter pushed down, unioned with
   warm⊎hot, then the existing DBSP consolidation collapses weights.

**Two-step vs one-step.** With standalone Parquet sidecars this is two in-process DuckDB queries (a
cheap probe to resolve live chunks, then the main read) — not network round-trips, so the cost is small.
The source design noted DuckLake can collapse this to one query because its catalog *is* a queryable
file-metadata table. **But** the merge base rides the flat layer on the DuckLake plane too (the
draw-transfer carve-out from `data-plane-ducklake.md`), so a single-step DuckLake path would require
*also* representing the base + sidecar as catalog rows (dual representation: flat for transfer, catalog
for local query). That trade is a **deferred refinement on the DuckLake plane**, explicitly not the
baseline — the flat two-step path is the plane-agnostic default.

## Settled decisions / deviations from the source design

- **Not a new storage model** — this extends the existing merge-main LSM cold base; warm/hot and
  reconstruct are unchanged.
- **Scoped to the merge-main cold base** (not "datasets" generally); append-only/overwrite excluded.
- **MASH is opt-in** (`cluster_by`), default = today's `_duckstring_f` ordering — no silent regression.
- **Sidecars are flat-Parquet companions, plane-agnostic** (built once in the base-publish path);
  DuckLake-catalog single-step is deferred.
- **One mandatory sidecar pair** (pk→chunk + chunk pk min/max), **no per-column secondary indexes** by
  default — they help least where MASH already clusters and add write-path cost at every compaction.
  A per-column index is a later, surgical exception only if profiling shows a recurring high-selectivity
  predicate on a *non-clustered* column zone maps can't prune.
- **Honest benefit framing**: segment elimination saves anti-join work on a full reconstruct and data
  read on point/predicate queries — not data read on a full scan.

## Open questions / for discussion

- **MASH vs freshness ordering of the base** (the Part-A decision point): accept relaxed as-of-on-cold
  for clustered tables, or fold `_duckstring_f` into the MASH key to keep partial freshness pruning?
- **`cluster_by` API shape** and how it interacts with `pk` (clustering ⊇ pk? independent?).
- **MASH key encoding in SQL**: generated arithmetic expression vs. a scalar UDF for the bit-interleave;
  bit-depth defaults and the `k·n ≤ 64` budget.
- **Bloom on the chunk sidecar**: ship min/max only first, add a bloom only if measured pk-range overlap
  warrants it.
- **Whether the sidecars travel on a cross-Catchment draw** — they're derived from the base, so a
  consumer could rebuild them on landing instead of shipping them; cheaper transfer, small recompute.

## Testing

- MASH ordering: a `cluster_by` table's base chunks prune correctly for predicates on each clustered
  column (zone-map row-group skip observed via `EXPLAIN ANALYZE`); a non-`cluster_by` table is
  byte-for-byte the current `_duckstring_f`-ordered base (no regression).
- Sidecars: pk min/max + pk→chunk map are correct after `checkpoint`, replay-idempotent, and rebuilt
  consistently after a cold compaction; point-lookup reads touch only the named chunk(s).
- Consolidation: reconstruct with sidecar-driven chunk elimination equals the un-accelerated reconstruct
  (same net Z-set), including the recency-clustered (low-overlap) and scattered-update (high-overlap)
  cases.
- As-of: confirm the chosen Part-A resolution's behaviour on a clustered base.
- All three planes serve a clustered merge main identically (the base + sidecars are flat-layer).
- `ruff check .` clean.
