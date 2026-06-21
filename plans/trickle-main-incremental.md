# Plan: incremental merge *main* — log-structured base + changelog

> **Status: implemented** (single-file base; chunked base + partition-granular checkpoint remain the deferred
> optimisations noted below). `apply_zset`/`merge_table` append to the changelog only; `reconstruct_sql` +
> `checkpoint` + the `f_base` meta live in `trickle/io.py`; reads reconstruct via `DataPlane.read_select`;
> the trigger + base publish are in `dataplane._checkpoint_and_publish_base` (`DUCKSTRING_COMPACT_THRESHOLD`,
> default 256 MiB); the data viewer reads freshness from the reconstructed main. Tests:
> `test_merge_main_checkpoint_folds_into_base` + the migrated merge/viewer suites.

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
- **Whole-base rewrite first.** The simple checkpoint rewrites the entire base (chunked, freshness-ordered).
  A *partition-granular* checkpoint (rewrite only the chunks holding changed PKs, locate via `_duckstring_f`)
  would shrink the per-checkpoint spike but needs a PK→chunk locator — deferred.

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

- **Partition-granular checkpoint** (rewrite only changed chunks via a PK→chunk locator) to remove the
  per-checkpoint O(base) spike — worth it only if the spike is measured to matter.
- **`compact_threshold` granularity** — catchment default with a per-pond override?
- **Who runs the checkpoint** — Duck inline at publish (simple, amortised, spiky) vs a background/quiesce
  sweep (smooth, a moving part). Lean inline.
- **As-of unification** — adopt as-of reads for `A_old`/`A_new` and delete `_reconstruct_old`, now that the
  source is log-structured? Tidy, optional.
