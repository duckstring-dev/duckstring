# Plan: incremental merge *main* — log-structured base + changelog

> **Status: log-structured main + chunked base implemented; tiered base (partition-granular checkpoint) is
> the next build — designed in "Tiered base" below.** `apply_zset`/`merge_table` append to the changelog
> only; `reconstruct_sql` + `checkpoint` + the `f_base` meta live in `trickle/io.py`; reads reconstruct via
> `DataPlane.read_select`; the trigger + base publish are in `dataplane._checkpoint_and_publish_base`
> (`DUCKSTRING_COMPACT_THRESHOLD`, default 256 MiB — now also a per-table override recorded at the merge
> write). The base is published as **size-bounded freshness-ordered chunks** (`{main}__base/`, DuckDB
> `FILE_SIZE_BYTES`) — see "Chunked base". The data viewer reads freshness from the reconstructed main.
> Tests: `test_merge_main_checkpoint_folds_into_base`, `test_chunked_base_splits_by_size_and_replaces_on_checkpoint`,
> `test_per_table_compact_threshold_overrides_catchment_default`, the draw/poller transfer tests, and the
> migrated merge/viewer suites.
>
> **Dropped:** the "as-of unification" tidy-up (use as-of reads for `A_old`/`A_new`, delete the builder's
> `_reconstruct_old`). On inspection it is a *regression*, not a tidy-up: `_reconstruct_old` computes
> `consolidate(current ⊎ −δ)` (delta-sized), whereas an as-of read reconstructs the full prior state before
> the key-filter — heavier on the incremental hot path (worse still with `key_filter=False`). Keep
> `_reconstruct_old`.

A follow-up to the append/changelog incremental-publish work (per-run parts — see the Data plane section of
`CLAUDE.md`). That work made every **append-only** published table (append history, `__changelog`,
`__droplog`) grow by O(change). The remaining O(table)-per-run costs are on the merge **main**, and there are
*two* of them:

1. the per-run **publish** of the whole main as Parquet (the expensive one — serialise + disk); and
2. the per-run registry **upsert** that keeps the main clean — its `DELETE FROM main WHERE pk IN (…)` scans
   the whole main every run (no PK index), even though only O(change) rows move.

This plan removes both by making a merge Trickle **log-structured**: the registry (and the published data)
hold a **base** (a checkpoint of the state up to a watermark `f_base`) plus the **changelog** (the Z-set of
everything after). Per run, only the changelog grows; the base is rewritten occasionally at a **checkpoint**.
Not yet implemented.

## The model

A merge Trickle is `(base, changelog)`:

- **base** — the consolidated current state as of `f_base` (one row per PK), carrying `_duckstring_f`
  (last-write freshness per row). Published as **size-bounded chunks** (~`compact_threshold`, default 256 MiB),
  freshness-ordered. Rewritten only at a checkpoint.
- **changelog** — the per-run Z-set parts (already incremental, from the append/changelog work),
  `_duckstring_f`-stamped.
- **`f_base`** — the **fold watermark**: the freshness up to which the changelog has been folded into the
  base. Recorded in the meta/sidecar.

Current state is reconstructed, never materialised per run:

```
current = latest-per-PK( base  ⊎  changelog WHERE _duckstring_f > f_base )
```

— take the base, overlay the changelog *strictly after* `f_base` (consolidated to the latest non-retracted
image per PK; retracted PKs dropped). The changelog `≤ f_base` is **already in the base** and must be
excluded here (or it double-counts); it is retained only so a lagging consumer can still window-read it.

### Reads

- **`read_delta` (the IVM common path)** — unchanged. It is the changelog window `(previous_f, f]`; it never
  touches the base. This is what the builder pulls every incremental run, so the hot path doesn't regress.
- **`read_table` / the comprehensive path (full current state)** — the reconstruction above. Bounded to
  `base + changelog-since-base`, which the checkpoint policy caps at ~2× base. Confined to comprehensive runs
  and genuine whole-table reads, not the incremental path.
- **Z-set shortcut** — where the builder wants a *state* for the join (`A_new` restricted to the affected
  keys), it can feed `base(+1) ⊎ changelog(>f_base)` straight in as a Z-set and let the join's output
  consolidation cancel the superseded versions — no separate latest-per-PK pass. (`A_old` stays the existing
  `current ⊎ −δ`, or equivalently an as-of read; the data plane's `as_of` seam already exists if we later
  want `A_old`/`A_new` purely via as-of reads and drop `_reconstruct_old`.)

### Per-run cost

| | per-run main publish | per-run main upsert (registry) | per-run total on the main |
|---|---|---|---|
| today | O(table) Parquet write | O(table) delete-scan | O(table) |
| this plan | — (changelog part only, O(change)) | — (no main maintenance) | **O(change)** |

The O(table) work happens only at a checkpoint, amortised.

## Checkpoint

Fold the changelog into the base and advance `f_base`:

```
target_f = latest freshness to fold (≈ now)
new_base = latest-per-PK( base ⊎ changelog WHERE _duckstring_f <= target_f )   -- drops dead versions + tombstoned PKs
write new_base ORDER BY _duckstring_f into ~compact_threshold chunks
f_base = target_f
```

- **Trigger (parameter-free):** checkpoint when `size(changelog since f_base) ≥ size(base)` (k=1). Self-tuning
  — write-amp ≤ 2×, reconstruction window ≤ 2× base — with no magic constant. `compact_threshold` (default
  256 MiB, catchment-level) is the **chunk size / floor**, not the trigger.
- **Amortised O(change)/run.** A checkpoint costs O(base) but fires only every ~`base/Δ` runs, so amortised
  ≈ O(Δ).
- **Lock-free — the key property.** Because every main read is **latest-per-PK** over `base ⊎ changelog`,
  rewriting/re-chunking the base is *idempotent* when a concurrent reader (or a draw) sees both the old and
  the new chunks: the same `(pk, _duckstring_f)` appears twice and the window function picks an identical row.
  So a checkpoint just writes the new chunks, swaps, and deletes the old — **no lock, no generation pointer**.
- **Whole-base rewrite first (done); tiered base next.** The shipped checkpoint rewrites the entire base
  into freshness-ordered chunks. The follow-up — removing the per-checkpoint O(base) spike — is the
  **tiered base** below: it does *not* use a per-PK→chunk locator (rejected: a small delta scatters across
  all freshness-ordered chunks, so any in-place partial rewrite degrades to O(base) anyway). Instead it
  defers deletes — append new freshness bands, supersede at read by latest-per-PK, reclaim lazily by
  compaction.

## Retention vs checkpoint — two independent axes

- **Retention** = the lag SLA: how far back the changelog is kept so a consumer can window-read before it
  must full-read. **Pond-owned** (`retain_t` / `retain_n`). Sets the **floor**; trims changelog `< floor`.
- **Checkpoint** = storage / write-amplification. **Catchment-owned** (`compact_threshold`, k=1).

They interact but don't merge: `floor ≤ f_base` (the base covers up to `f_base`; the changelog is retained
from `floor` for laggards; the `[floor, f_base]` slice is in *both* the base and the changelog, which is why
reconstruction filters `> f_base`). A checkpoint is a convenient moment to also run the retention trim, but
the cadences are governed separately — don't tie checkpoint cadence to the retention window, or a long SLA
(e.g. 30 days) would let the changelog grow enormous before basing.

## Partitioning order

- **Default: freshness.** It's emergent and free — the checkpoint writes `ORDER BY _duckstring_f` into
  size-bounded chunks, and recency correlates with read locality. For monotonic keys, freshness ≈ PK order.
- **Reject PK-hash** as a default: it shreds the natural locality of real keys (monotonic ints / time-ish
  codes), so a key-range filter or sort-merge join prunes nothing; hash only helps point lookups and even
  load spread — not the scan-heavy analytics case.
- **`.order_by(col)` — future knob, not now.** A custom clustering key turns the checkpoint into a
  sorted-merge-with-splitting (LSM leveling) — real work. It is also the prerequisite for *order-dependent*
  aggregates (first / last / cumsum / lag), which are a separate, much harder beast incrementally (no group
  homomorphism; a cumsum change ripples to every later row) and are out of scope.

## Chunked base (implemented)

The published base is a directory of size-bounded, freshness-ordered Parquet **chunks** —
`{main}__base/{token}__{i}.parquet`, written by `dataplane._publish_base_chunks` from the registry base
(`COPY … ORDER BY _duckstring_f … FILE_SIZE_BYTES <threshold>`). So a single base holds far more than one
Parquet file's worth (the large-table / small-delta case), and the chunk size is the `compact_threshold`.

- **Read** — `ParquetDataPlane._raw_read_select` is base-dir-aware (`read_parquet('{main}__base/*.parquet')`
  + the as-of predicate); Iceberg inherits it via its non-catalog flat-read fallback; the viewer/reconstruct
  flow through `read_select` unchanged.
- **Lock-free swap** — each checkpoint writes its chunks under a fresh `token` (the run `f`), then drops the
  previous token's chunks. A reader that momentarily sees both reconstructs latest-per-PK over base ⊎
  changelog (idempotent); the published sidecar `f_base` advances only *after*, so the changelog still
  covers any row a stale chunk would resurrect.
- **Transfer** — `part_tables` excludes `__base` (the wholesale base must never be mistaken for incremental
  per-run parts / counted by `landed_after`); the draw ships every base chunk wholesale; the poller
  wholesale-replaces the base dir on land (pruning stale-token chunks, else a deleted PK resurrects
  downstream). The **tiered base** below makes this transfer incremental.

## Tiered base (partition-granular checkpoint)

The remaining O(base) cost is the checkpoint's whole-base rewrite. A per-PK→chunk locator can't remove it:
because chunks are freshness-ordered and a small delta's PKs are uncorrelated with *freshness order* (though
they *are* correlated with **time**, and freshness ≈ time — see below), an in-place partial rewrite scatters
across all chunks → O(base). The fix is to **defer deletes** and let reads supersede + compaction reclaim —
i.e. make the main a small log-structured merge tree, with the changelog as its bottom level.

**Three tiers (the main as an LSM):**

- **L0 — hot changelog.** The per-run `{main}__changelog/{f}.parquet` parts (unchanged). `read_delta` (the
  incremental hot path) still reads only these over its window — untouched.
- **L1 — warm bands.** Freshness-partitioned Z-set bands (carry `_duckstring_d`, incl. `−1` tombstones that
  suppress older tiers). A **warm merge** folds L0 → a warm band and merges adjacent warm bands at the chunk
  threshold; cost O(recent change). Because updates are time-correlated, recent churn cancels **locally**
  (a re-updated recent PK's prior image is also in the warm region → `−old/+new` annihilate → no tombstone
  reaches cold).
- **L2 — cold base.** The strictly-`d=+1`, one-row-per-PK clean base (the chunked base above). Rewritten
  only by a **cold compaction**.

**Reconstruct (cold stays clean ⇒ never grouped):** because L2 is single-version by construction, the read
keeps today's `reconstruct_sql` shape, generalised to tiers — consolidate only warm⊎hot (the small changed
set), then anti-join cold by the **retraction keys** and union the present side:

```
tombstone_keys = DISTINCT pk WHERE d < 0  over (warm ⊎ hot)        -- small
current = ( cold WHERE pk NOT IN tombstone_keys )                  -- a scan, never a GROUP BY
        UNION ALL
          ( consolidate(warm ⊎ hot) WHERE net d > 0 )              -- GROUP BY over the changed 5%, not 95%
```

The anti-join keys *are* exactly the retraction keys (a delete is `−old`; an update is `−old/+new`, same PK;
a pure insert has no `−1` and isn't in cold). The 95% cold majority is a plain scan (unavoidable on a full
read — you return those rows) but never grouped. Don't split warm deletes into a separate file (the present
side already reads warm⊎hot, so extracting `d<0` keys from that pass is ~free); instead carry a cheap
per-band **`has_deletes`** (and delete-key min/max) stat in the manifest so reconstruct skips retraction-key
extraction for insert-only bands.

**Compaction triggers (self-tuning, no new constant — the existing k=1, applied per tier):**

- **Warm merge** — frequent/cheap: at the chunk threshold, fold L0 + merge adjacent warm bands.
- **Cold compaction** — rare/expensive: when **total warm ≥ cold base** (k=1, the same invariant as today's
  `changelog ≥ base`), merge warm+cold → a fresh clean cold base. Temporal locality keeps warm small (local
  cancellation), so this fires roughly **proportional to total data size** — ~never for settled data, and
  exactly when old-data churn has accumulated enough dead weight to justify reclaiming it. (Imprecision: the
  size trigger can't tell insert-growth from old-PK-tombstone growth, so a pure-append workload occasionally
  re-chunks unchanged cold data — same wasted rewrite the current whole-base checkpoint already does, so no
  regression; a "tombstones whose `f` predates the warm floor" counter would make it precise without a
  per-PK map, if measured to matter.)

**Manifest (per-band `f`-range + `has_deletes`) does triple duty:** band-skip for windowed/as-of reads,
retraction-key pruning for reconstruct, and **incremental base transfer** — the draw ships only the bands a
consumer's manifest lacks (by `f`-range), retiring the wholesale-base draw above.

**Build order:** (1) banded reconstruct + warm/cold compaction (registry + publish), keeping the suite
green; (2) the manifest-driven incremental transfer (draw/poller).

## Append / changelog / droplog compaction — deferred

The same *size* policy could merge their accumulated per-run parts into ~`compact_threshold` files, but:

- it is pure **concatenate**-compaction (no base, no fold);
- it is **low value** — append/changelog windowed reads already prune files by `_duckstring_f` min/max
  stats, so file *count* is mostly a directory-listing cost; and the main's reconstruction window is already
  bounded by the checkpoint;
- it **lacks the main's idempotent-overlap safety** — these reads `UNION` / `SUM(_duckstring_d)`, so a
  transient old+new overlap during a file-merge double-counts; it would need a generation-safe swap or a
  `(row, _duckstring_f)` read-side dedup.

So ship the **main checkpoint** now (the actual win, naturally lock-free) and leave append/changelog/droplog
compaction as a later, optional tidy-up.

## Surface changed

- **`apply_zset`** — drop the main CoW-upsert; write only the changelog. (The base is touched only at
  checkpoint.)
- **Data plane `read_select` for a merge main** — reconstruct `latest-per-PK(base ⊎ changelog > f_base)`
  instead of a plain file scan; base chunks read via the parts machinery.
- **Checkpoint op** — new; consolidate `base ⊎ changelog ≤ target` → chunked freshness-ordered base, advance
  `f_base`, run retention trim. Triggered at publish when `changelog ≥ base` (k=1). Run by the Duck inline
  (amortised, its own cost) — or at quiesce if the spike matters.
- **Meta / sidecar** — record `f_base`; exempt the base's `_duckstring_f` from the reserved-column publish
  check and the schema contract (as the changelog already is).
- **Builder** — largely unchanged: it still calls `read_table` (now reconstructing) and `read_delta`
  (unchanged). The Z-set / as-of optimisations above are optional.

## Open questions

- **Partition-granular checkpoint** — *resolved*: the **tiered base** above (defer deletes → append warm
  bands + merge-on-read + k=1 cold compaction), not a per-PK→chunk locator.
- **`compact_threshold` granularity** — *resolved*: catchment default (`DUCKSTRING_COMPACT_THRESHOLD`) +
  a per-table override recorded at the merge write (`merge_table(..., compact_threshold=)`).
- **As-of unification** — *dropped* (a regression, see Status).
- **Who runs the checkpoint** — Duck inline at publish (simple, amortised, spiky) vs a background/quiesce
  sweep (smooth, a moving part). Lean inline.
- **Precise cold-compaction trigger** — a "tombstones whose `f` predates the warm floor" counter would
  distinguish insert-growth from old-PK-tombstone-growth, sparing the occasional unneeded cold re-chunk.
  Deferred until the size-proxy trigger is measured to misfire.
