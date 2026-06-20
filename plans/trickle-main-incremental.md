# Plan: incremental publish of the merge *main*

A follow-up to the append/changelog incremental-publish work (per-run parts — see the Data plane section of
`CLAUDE.md`). That work made every **append-only** published table (append history, `__changelog`,
`__droplog`) grow by O(change). The one remaining O(table)-per-run write is the merge **main**: it is the
clean current state (one row per PK, `SELECT *` = the table), published by overwriting `{main}.parquet`
wholesale every run. For a large dimension with localised changes this is the dominant run-completion
latency cost. This plan is the way forward; it is **not yet implemented**.

## The two proposals are the same idea

Both proposals turn the main into a **log you integrate on read** (merge-on-read), differing only in where
the per-run delta comes from:

1. **Changelog-informed upsert** — apply the run's Z-set (already materialised in the `__changelog`) to the
   published main as a row-level upsert: delete the touched PKs, insert their new images.
2. **`_duckstring_f` on the main** — stamp each main row with the freshness at which it was last written, and
   publish only `… WHERE _duckstring_f >= f` (the rows this run touched).

They carry the same information. Crucially, **neither is complete on its own for deletions**: a deleted PK
has *no* row in the clean registry main, so "publish the changed rows" never emits anything for it, and a
naive latest-per-PK reconstruction on the consumer would **resurrect** the deleted row from an older part.
A delete must be published as an explicit **tombstone**, and the only place the deletion is recorded is the
changelog (the `-1` retraction). So proposal 2 still needs the changelog's deletes — it converges with
proposal 1. The real design question is not "which delta source" but **what substrate** carries a
merge-on-read main.

## What a merge-on-read main needs (independent of substrate)

- **Per-run delta of the main** — the changed/new rows (`_duckstring_f >= f`) *and* the deleted PKs
  (tombstones from the changelog's retractions). O(change) to produce.
- **Read-time reconstruction** — current state = the latest non-tombstoned image per PK across the parts:
  `… QUALIFY row_number() OVER (PARTITION BY pk ORDER BY _duckstring_f DESC) = 1`, with tombstoned PKs
  dropped. O(rows across live parts).
- **Compaction** — periodically collapse the parts back into a fresh base so read cost (and part count)
  stays bounded. A compaction re-introduces one O(table) write, but amortised over many runs.

The cost trade vs today: wholesale is **O(table) write / run, O(table) plain-scan read**. Merge-on-read is
**O(change) write / run, O(live-parts) read + periodic compaction**. The win is real because **incremental
runs do not full-read the main** — the consumer reads the *changelog* delta, not the main. A full main read
happens only on the **comprehensive** path (bootstrap / coverage-miss / changed-overwrite / over-`p`) and on
a genuine whole-table `read_table`, so the slower reconstruction read is paid on the exception, not the
common path. That asymmetry is what makes trading a guaranteed per-run write for an occasional heavier read
a good deal.

## Recommendation

**Build the main as a parts-based merge-on-read table, reusing the per-run-parts machinery already in the
flat layer** (proposal 2's substrate, made delete-correct with changelog tombstones):

1. **Stamp the registry main with `_duckstring_f`** (the last-write epoch per row). This is the portable
   artifact proposal 2 wants — it also directly answers "what main rows changed since `f`?" without the
   changelog, and is the seam any future plane needs. (Note: this relaxes the current "merge main is pure
   user columns" invariant — `read_table` already strips `_duckstring_*`, so consumers are unaffected, but
   the publish/contract code that special-cases the main must learn to exempt it like the changelog.)
2. **Publish the main as per-run parts**: `main/{f}.parquet` = the rows with `_duckstring_f = f` (upserts)
   **plus** tombstone rows for the PKs the changelog retracted this run (a `_duckstring_d = -1` marker
   carrying just the PK). Exactly the `_export_parts` shape we already have, with a tombstone union.
3. **Reconstruct on read** in `read_select`/`read_table` for a merge main: latest-per-PK over the parts,
   dropping PKs whose latest row is a tombstone.
4. **Compact** when the part count (or live/total row ratio) crosses a threshold: rewrite a single base
   part at the current `f` and drop the rest. Amortised O(table); the only wholesale write, now occasional.

This reuses what we just built, stays **plane-agnostic** (flat Parquet benefits too, not just Iceberg),
keeps the producer's per-run write O(change), and confines the heavier read to the comprehensive path.

### The Iceberg alternative (and why not to lead with it)

On Iceberg the same merge-on-read is expressible natively — **equality-deletes + append** (write the changed
PKs as a delete file + the new images as a data file, O(change), reader merges), or **copy-on-write upsert**
(`tbl.upsert()` / `overwrite(filter=…)`, rewrites only the data files containing a touched PK). CoW gives
plain-scan reads but its write is O(touched *files*) — for scattered PK changes that degrades to O(table)
(it rewrites every file), so it does **not** guarantee the latency win; equality-delete MoR does, but
pyiceberg's *writing* of equality deletes is less mature than its reading. Leading with Iceberg would also
couple main incrementality to one plane and not help the flat opt-out. Keep the Iceberg-native path as a
later optimisation (fast reads via CoW where changes are localised, or equality-deletes once pyiceberg's
MoR-write support is solid), layered under the same `_duckstring_f`-on-main metadata.

## Open questions

- **Compaction policy** — by part count, by live/dead ratio, by age? And who runs it (the Duck at publish,
  or a background sweep)? A bad cadence either lets reads rot or re-introduces frequent O(table) writes.
- **Tombstone lifetime** — a tombstone can be dropped once no live part predates it for that PK (i.e. after
  the next compaction). Until then it must travel in the draw like any other part.
- **As-of reads** — latest-per-PK is "as of latest". An as-of-`f` read (the data plane's `read_select(...,
  as_of=)` seam) becomes "latest per PK with `_duckstring_f <= f`" — naturally supported by the stamp, a
  nice bonus of proposal 2.
- **Contract/àpublish** — `_duckstring_f` on the main must be exempted from the reserved-column publish
  check and the schema contract, exactly as the changelog already is.
- **Draw** — main parts ship like changelog parts (newer-than-`after`), but the consumer must apply the
  same latest-per-PK reconstruction; `landed_after` already covers part-tables generically.
