# Data plane: cold-tier clustering (MASH) + needle search for merge mains

Status: **proposed (2026-06-28)**, not implemented. **Sequenced after `plans/data-plane-ducklake.md`**
and **builds directly on `plans/trickle-main-incremental.md`** (the merge-main LSM). This is a
read-acceleration layer on the merge-main **cold base** — it does not change the freshness/Z-set
semantics, the reconstruct correctness, or the per-run O(change) publish.

## What this is — and what already exists

The source design described a "two-tier storage with sidecar skip structures." **Duckstring already has
the two tiers** (three, actually — see `trickle-main-incremental.md`), so the only *new* ideas here are
**how the cold base is ordered + written at compaction** so that DuckDB's *native* Parquet pruning
serves both needle (pk) and mid-scale (`order_by`) reads — without any external secondary index. (The
source design's two sidecars were dropped on reflection; see Part B.) Mapping the source design's
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
**no secondary index of any kind**. This plan changes how that base is *ordered and written* at the
existing `checkpoint`/`_publish_base_chunks` pass — the one moment a full immutable cold partition is in
hand and row positions are free to move — so DuckDB's native Parquet pruning serves needle and mid-scale
reads. No external secondary index is built (see Part B).

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
(the base rides the flat layer for the cross-Catchment draw). So **the ordering + bloom changes are
implemented once, in the Parquet base-publish path** (`_publish_base_chunks`), and all three planes
inherit them with no plane-specific work — and because the baseline leans on DuckDB's native Parquet
pruning (no external index to probe), the read is **one step on every plane** (see Part C).

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

**NULLs** are handled by computing the quantiles over **non-NULL** values only and mapping a NULL to a
**reserved top bucket** (all-1s code, above the max non-NULL bucket). This clusters all NULLs together
(so `IS NULL` / equality predicates prune the NULL region) without letting a high NULL fraction skew the
non-NULL quantile boundaries. (Folding NULLs into the quantile estimate as a max-sentinel — the
originally-suggested approach — also works, but skews the boundaries when NULLs are common; the reserved
bucket avoids that.)

### Where it hooks, and the two ordering modes

`_publish_base_chunks` (`dataplane.py`) currently does one `COPY (SELECT * FROM "{main}" ORDER BY
_duckstring_f) … (FILE_SIZE_BYTES …)`. The base is rewritten only here (at `checkpoint`), so this is the
one place ordering is chosen. **Order key columns are declared on the merge write** — a new optional
`cluster_by=[…]` on `merge_table(…)` / `.merge(…)`, recorded in the `_duckstring_trickle` meta + the
`_trickle.json` sidecar (alongside `mode`/`pk`/`floor`/`f_base`). `cluster_by` **may include pk
columns** (a timestamp pk is often the best order column) but need not. Two modes:

- **No `cluster_by` → `ORDER BY pk`** (the new default; replaces the current `_duckstring_f` order).
  Freshness ordering bought only as-of-on-cold pruning, which is near-useless (a merge base is folded up
  to `f_base`, so as-of *below* `f_base` is unsupported anyway, and as-of *≥* `f_base` reads the whole
  base) — so pk-order is a strict improvement: chunks get **disjoint pk ranges**, and a pk needle search
  is served *for free* by DuckDB's native per-row-group min/max stats (footer-level), no index needed.
- **`cluster_by` given → `ORDER BY mash_key`**, plus **pk bloom filters** on the written chunks (see
  Part B). MASH gives the zone-map pruning for `order_by`-column filters (the mid-scale band); the pk
  bloom recovers the needle search that MASH's pk-scatter would otherwise lose.

## Part B — needle search (pk) without an external index

The original design proposed two external sidecars (a per-chunk pk min/max table and a pk→chunk→ordinal
map). **On reflection, both are dropped from the baseline** — DuckDB's *native* Parquet machinery
already does the job once the chunks are written well, which keeps to the "no engine control, SQL
composition only" spirit and avoids maintaining a secondary index at every compaction:

- **pk-ordered base (no `cluster_by`)**: chunks have disjoint pk ranges, so the **native footer min/max
  stats** prune a pk-equality probe to one chunk, then to one row group within it. No sidecar — the
  pk→chunk map and the per-chunk min/max table would both be re-deriving what the Parquet footers
  already carry.
- **MASH-ordered base (`cluster_by`)**: pk is scattered across all chunks (interleaving gives pk only
  its *high* bits near the top of the composite — not enough to isolate one pk), so range pruning can't
  find a needle. Recover it with **per-row-group Parquet bloom filters on the pk column**, written into
  the chunks at `COPY` time. DuckDB **reads Parquet bloom filters natively** for row-group pruning, so a
  pk probe checks each chunk's blooms and scans only the row group(s) that can contain the pk. **Write
  the bloom even when pk ∈ `cluster_by`** — MASH still scatters it enough that the bloom, not the
  ordering, is what makes the needle fast.

**The one thing the spike must confirm:** that DuckDB's Parquet *writer* emits column bloom filters (it
definitely *reads* them) and the `COPY` syntax to request one on the pk column. If the writer can't, the
fallback is a small **external per-chunk pk bloom** companion (flat Parquet, reserved-namespace name
next to `{main}__base/`, rebuilt each `checkpoint`) that the query-construction layer probes to narrow
the chunk glob — i.e. the MASH case (only) falls back to a two-step read; the pk-ordered default stays
single-step regardless.

### Does the engine need to know the pk is unique?

No, and it doesn't meaningfully matter for search. Min/max and bloom pruning are identical on unique vs.
non-unique columns; the only thing uniqueness could buy is **early termination** (stop scanning the
matched row group after the one row), which DuckDB does not take a hint for on Parquet scans anyway — so
the saving is a fraction of a single row-group scan, negligible against the pruning that already
isolated that row group. **Row-group skipping dominates so completely that unique-vs-non-unique is
second-order.** Duckstring knows the pk from the merge declaration (the reconstruct uses it), but the
cold-base read path needs no uniqueness input.

### What this means for the consolidation anti-join

The reconstruct anti-joins the cold base by the warm⊎hot **retraction keys** (`reconstruct_sql`). Note
the asymmetry the source design blurred: for a **full** table read, native pruning saves nothing — every
cold chunk is part of the current state and must be read and returned. The needle/predicate wins
(eliminating chunks/row groups) land on **point and `order_by`-filtered reads**, which is exactly the
target (needle + mid-scale). The design bets on **recency-clustered changes** (edits cluster near create
time → warm pks recent, cold pks old → low overlap), and degrades under scattered historical updates
(backfills, GDPR sweeps) — the known boundary, not a bug.

## Part C — pulling it into queries (no engine control)

Pure SQL composition at the orchestration layer (the `read_select`/`reconstruct_sql` construction we
already own) — no DuckDB extension, no scan-operator hacks. The read is just
`read_parquet('{main}__base/*.parquet')` with the predicate (pk equality or an `order_by` filter) pushed
down, unioned with warm⊎hot, then the existing DBSP consolidation collapses weights. **DuckDB prunes
chunks and row groups itself** from the Parquet footer stats (pk-ordered min/max, or MASH zone maps) and
the native bloom filters — so the baseline is **a single query on every plane**, with no external probe.

The only path that costs a second step is the MASH-case bloom *fallback* (if the DuckDB writer can't
emit blooms): then the query-construction layer first probes the external per-chunk bloom companion to
narrow the chunk glob, then reads. Both queries are in-process (no network round-trips), so even the
fallback is cheap — and the DuckLake catalog could later serve that probe inline (a deferred refinement,
not the baseline; it would mean dual-representing the base as catalog rows, which fights the flat-layer
draw-transfer carve-out, so it's explicitly out of scope here).

## Expected performance (honest estimates)

At **GB scale** (a 1–10 GB base → tens of 256 MiB chunks):
- **Needle (pk equality), warm metadata** (footers cached, which DuckDB does): realistically a
  **few-to-~10 ms** — prune to one chunk/row group and scan it.
- **Needle, cold metadata** (first query, footers not cached): can be **tens of ms** because DuckDB opens
  every chunk's footer to read stats/blooms (footer fan-out across tens of files). This is the *one*
  case an external per-chunk min/max companion (or a DuckLake catalog) would collapse to a single small
  read — so if cold-query latency proves to matter, that's the lever, available without redesign. It is
  *not* needed at GB scale with tens of chunks, which is why it's deferred.
- **Mid-scale `order_by` filter** (1–10 % selectivity on a clustered column): **tens of ms** — scan the
  selected fraction of row groups; MASH's *approximate* clustering means pruning isn't perfect (some
  bleed across the interleave). "Quite quick," matching the goal, but framed honestly as looser than the
  needle target.

So ~10 ms needle is a reasonable target with warm metadata; mid-scale lands in tens of ms. The
cold-metadata footer fan-out is the known caveat, with the deferred companion as its remedy.

## Settled decisions / deviations from the source design

- **Not a new storage model** — extends the existing merge-main LSM cold base; warm/hot and reconstruct
  are unchanged.
- **Scoped to the merge-main cold base** (not "datasets" generally); append-only/overwrite excluded.
- **No external sidecars in the baseline** — lean on DuckDB-native Parquet stats (pk-ordered min/max) and
  native bloom filters. An external per-chunk pk bloom is a *fallback* only if the DuckDB writer can't
  emit blooms; an external per-chunk min/max is a *deferred* optimization only for the cold-metadata /
  high-file-count regime. (This replaces the source design's two mandatory sidecars.)
- **Default ordering is pk, not freshness** — freshness ordering protected only an as-of-on-cold read
  that is near-useless on a folded base, so pk-order (free needle search via native stats) is strictly
  better.
- **`cluster_by` is opt-in and may include pk columns**; absent it, the base is pk-ordered. MASH +
  zone-map clustering is bought only where a table's read pattern asks for it.
- **No per-column secondary indexes** by default — they help least where MASH already clusters and add
  write-path cost at every compaction. A per-column index is a later, surgical exception only if
  profiling shows a recurring high-selectivity predicate on a *non-clustered* column zone maps can't
  prune.
- **NULLs** → quantiles over non-NULLs + a reserved top bucket.
- **Uniqueness is not an engine input** — row-group skipping dominates; unique-vs-non-unique is
  second-order for search.

## Open questions / for discussion

- **DuckDB Parquet bloom-filter *writer*** — confirm `COPY` can emit a per-column bloom on pk (the whole
  no-external-index baseline for the MASH case rides on this); else wire the external-bloom fallback.
- **`cluster_by` API shape** — column list on `merge_table`/`.merge`, and whether pk-in-`cluster_by`
  needs any special handling beyond "still write the pk bloom."
- **MASH key encoding in SQL** — generated arithmetic expression vs. a scalar UDF for the bit-interleave;
  bit-depth defaults and the `k·n ≤ 64` budget.
- **Cold-metadata footer fan-out** — measure whether GB-scale needle latency on a cold cache actually
  needs the external per-chunk min/max companion, or stays within target on its own.

## Testing

- Ordering: a no-`cluster_by` table's base is pk-ordered with disjoint per-chunk pk ranges; a needle
  read touches one chunk/row group (verify via `EXPLAIN ANALYZE` pruning counters). A `cluster_by`
  table's chunks prune for predicates on each clustered column.
- Bloom: a MASH-ordered table's pk needle prunes to the containing row group(s) via the bloom (native or
  the fallback companion); an absent pk reads no data rows.
- Consolidation: reconstruct with native pruning equals the un-accelerated reconstruct (same net Z-set),
  in both the recency-clustered (low-overlap) and scattered-update (high-overlap) cases.
- Regression: the default-ordering change from freshness→pk does not alter reconstruct results or as-of
  reads at/above `f_base`.
- All three planes serve a clustered merge main identically (the base is flat-layer).
- `ruff check .` clean.
