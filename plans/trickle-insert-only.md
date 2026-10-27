# Plan: a fast path for insert-only changes, and the builder's other per-run waste

Status: proposed (2026-10-25). Nothing here is built.

## Problem

The TPC-DS chain benchmark (duckstring-blog `bench/tpcds_chain/`) absorbs a batch of new sales, 0.5% of the fact,
in 21.7 s at SF1000 on an r7gd.8xlarge, against 166 s for DuckDB's rebuild. Every step of that change is new
rows only, yet it takes far longer than joining and aggregating 13.4M rows should.

Per Pond at SF1000 (seconds from the Refresh to each Pond finishing; the three Outlets run in parallel):

| Change | ingest | `sales_enriched` | `category_month`, `store_quarter`, `customer_value` | total |
|---|---|---|---|---|
| New sales | 2.7 | 12.7 | 17.2, 17.6, 21.7 | 21.7 |
| Fact | 3.5 | 18.9 | 23.1, 23.3, 29.9 | 30.0 |
| Mixed | 3.5 | 30.5 | 36.7, 36.6, 44.8 | 44.9 |

`store_quarter` absorbing 13.4M new rows (4.9 s) costs nearly what DuckDB takes to aggregate all 2.7 billion
(6.3 s).

### Profile (2026-10-25, SF100 on the laptop, new-sales case, 1.34M new sales)

Measured with a timing shim on the Ducks (a `sitecustomize` wrapping the Trickle, data plane and executor
functions; totals within 5% of unprofiled runs) and `EXPLAIN ANALYZE` of each join query.

`sales_enriched`, 1.5 s:

| Step | s |
|---|---|
| read the new sales' delta into a temp table | 0.15 |
| three intermediate join deltas materialised (`_scratch_table`) | 0.39 |
| the final join query, the root consolidation and `apply_zset` (its own consolidation, the change log insert) | 0.50 |
| `_chain` materialising the output delta for a chain step that never comes | 0.11 |
| Ripple overhead (compile, views) | 0.12 |
| publish (the change log as one Parquet file) | 0.19 |

Every join query together took about 0.3 s of wall time. The dimension joins themselves were tens of
milliseconds each; DuckDB hashes the dimension once and streams the delta through it. The final query's root
consolidation, a hash group-by over all 39 columns of every leaf (the output keeps 18), was about 80% of that
query. The left joins (customer, address, returns) have an unchanged right side, but each is computed as a
matched term plus a padded anti term, so `_reads_twice` materialises the delta below it.

`category_month`, 1.08 s: reading `enriched`'s current state 0.33 (never used by a sum/count aggregate), reading
its delta 0.33, counting it for the `p` check 0.10, aggregating 0.17, writing 0.03, publish 0.03.
`store_quarter` is the same. `customer_value`, 1.86 s: the current state 0.57 (read twice: once by
`_compile_source`, once by `_full_join` for a `max` rescan an append can't need), delta 0.32, aggregating 0.39,
writing its change 0.22, publish 0.13. In the fact and mixed cases its aggregating rises to 1.1 to 1.35 s, the
`max(d_date)` rescan after retractions, which is genuine work.

At SF1000 the same steps take 4 to 5 times as long (ten times the rows on 3.2 times the cores), and the one-file
change log publish grows to 2.2 s.

## Part 1: waste on every run

These apply whatever the change contains.

1. **Don't read a Source's current state when nothing uses it.** `TrickleBuilder._compile_source` calls
   `ctx.read_table` up front, and for a merge Source that runs `materialise_reconstruct` at once (the change log
   consolidation into a temp table). Decide at compile time, once the deltas are known, whether any term, the
   comprehensive path or a rescan will read it, and skip it otherwise. An aggregate over one Source with no
   rescan then never touches it, and `customer_value` stops reading it twice. Deciding at compile time (rather
   than reading on first use) keeps every read as early as today; see "Freshness and concurrent runs".
2. **A left join whose right side is unchanged is one pass.** With `δR` empty, `δ(L ⟕ R) = δL ⟕ R'` for any
   `δL`, retractions included (the left join is linear in `L` for a fixed `R`). Likewise right joins with `L`
   unchanged, and semi/anti with `Q` unchanged (`δP ⋉ Q'`, `δP ▷ Q'`). A full join isn't linear in either side
   (a left row can un-pad a right row), so it keeps its terms. This drops the padded anti term and, with it, the `_reads_twice` scratch tables.
3. **Consolidate once, after the projection.** `_compute` consolidates the root over every leaf column, then
   `apply_zset` consolidates again over the output columns. The pipeline (`filter`/`mutate`/`select`) is
   row-local and deterministic, so it commutes with consolidation: run it first, consolidate the projected rows
   once, and pass `apply_zset` a flag that the input is already consolidated. `.accumulate().append()` still
   sees a consolidated input.
4. **Prepare a chained step's delta lazily.** `_chain` calls `read_registry_delta` (a materialised copy of the
   change just written) whether or not anything chains. Defer it to the first use of the returned builder.
5. **Count a Source from metadata.** The `p` check's `consolidated_count_select` sums `_duckstring_d` over the
   change log parts. Use footer row counts where the parts are insert-only (Part 2), else keep the scan.
6. **Make a large change log part quicker to publish.** Measure a single-file write first (the `COPY` of a
   Z-set part with `preserve_insertion_order = false`, since a part's row order means nothing). Chunking the part
   as append history does (`_export_parts(chunk=True)`) breaks the "one file per run" invariant several readers
   rely on, so it comes only if the single file is still too slow, and only after Stage 0 and these:
   - the warm fold keeps the newest *run*, every chunk of it (today `hot[:-1]` in
     `dataplane._publish_tiered_main` keeps the newest *file*, so with chunks it would fold the newest run and
     every caught-up consumer would miss coverage and read the table in full);
   - a Draw ships only runs whose chunks are all in place (see "Existing hazards"), and an older consumer that
     can't parse `{f}__{i}` names refuses the transfer rather than misreading it.

## Part 2: the insert-only fast path

A streaming Source (new rows only) should run as close to "join once, write once" as possible, and a run with
updates or deletes should fall back to today's path for the part of the pipeline they reach.

### Detecting it

Per Source, per run, without reading the change:

- An append Trickle is insert-only by contract (sidecar `mode`).
- A merge Trickle's window is insert-only when every change log part in `(previous_f, f]` has a footer minimum
  of `_duckstring_d` at least 1 (Parquet column statistics, a metadata read; warm bands are consolidated Z-sets
  and carry the same statistics). In a consolidated merge log an update always pairs its `+1` with a `-1`, so an
  all-positive window means every row is a new key. `merge_table` and the builder keep that invariant;
  `apply_zset` callers already must, for joins to be correct.
- An in-run chained delta (`read_registry_delta`) carries the flag in memory from the write that produced it.
- An unchanged Source has no delta. A full read (bootstrap, coverage miss, a changed overwrite Ripple) is the
  comprehensive path, as today.

`Delta` gains `insert_only`, so a hand-written Ripple can use it too.

### Propagating it

Each `_NodeState` gets `insert_only`, by which operations only add output rows when their inputs only add rows:

| Node | insert-only when |
|---|---|
| inner, semi | each side insert-only or unchanged |
| left | left insert-only, right unchanged (a new right row can match a padded left row and retract it) |
| right | the mirror |
| anti | left insert-only, right unchanged |
| full | never while either side changes (an insert on one side can retract the other's padded row) |
| `filter`, `mutate`, `select` | the input is |
| `aggregate`, `.sql()` | never (an aggregate updates existing groups; `.sql()` is opaque) |

The delta of an insert-only node has no negative terms:

- inner with only `L` changed: `δL ⋈ R'`; with both: `δL ⋈ R' + L_old ⋈ δR`, where `L_old` is `L'` restricted to
  `_duckstring_f <= previous_f` (for an append Source a filter; for a merge Source the reconstruct before `f`);
- left with `R` unchanged: `δL ⟕ R'` (Part 1, item 2);
- semi: `δP ⋉ Q'` plus, when `Q` inserted, the `P_old` rows that gained their first match (a flip set with only
  positive flips).

### Using it

- **No consolidation at the root.** Every row is `+1`; nothing can cancel.
- **`apply_zset` writes the rows straight into the change log.** The risk is a `+1` for a key the table already
  holds, which reconstruction would read as a silent replacement. It is safe without a check when the output key
  is a verbatim pass-through of an insert-only spine's key (`_spine_pk_passthrough`, which already exists), the
  streaming case. Otherwise check the new keys against the table's current keys (an anti-join on the key
  columns) or take the general path; measure which is cheaper.
- **The published part says so.** Its footer minimum of `_duckstring_d` is 1, so the next Pond sees an
  insert-only window and:
  - reads its delta without consolidating it. Reading the parts in place, with no temp copy, needs Stage 0's
    snapshots: today the temp copy is what pins the delta's rows at compile time, and a read deferred to later
    in the run can find its parts folded into a band and deleted by the Source's next run;
  - reconstructs the current state, where it needs it, as base plus parts, with no anti-join (same condition);
  - counts it from footers.
- **An aggregate fed insert-only** folds sums in only (no partition moment merged out) and never rescans an
  extreme. Its own output is an update per affected group (`-1` old, `+1` new), so the Pond after it takes the
  general path; that is correct, not a loss.

### Writing once (later)

An insert-only change can be written straight to Parquet chunks, with the registry's change log a view over
them, as a bootstrap is staged today (`stage_base`, `.boot/`). That replaces "insert into the registry, then
export" with one parallel write.

### Splitting mixed changes (later)

The fact case is mostly inserts (538k new sales against 180k corrections and voids at SF100). A merge Source's
delta can be split by key: rows whose key has no `-1` in the window go down the fast path, the rest down the
general one, and only the general part's output is consolidated. Dimension changes (the mixed case's moved
customers and items) still need their scans of the fact; that is real work.

## Freshness and concurrent runs

What each read is, today. A Sink run at freshness `f` reads a Source table over the window `(previous_f, f]`
(its delta) and as of `f` (its current state). A Source merge table is read as globs (`dir/*.parquet`,
`dataplane._flat_read_select`) resolved when each query executes, with the sidecar's `f_base` and floor read
once at compile. A Source plain table is pinned to a version (`source_pins`, `retain_from`). The delta and the
current state's change log consolidation are copied into temp tables at compile; the cold base stays a lazy
glob. The Sink runs only after the Source's Run at `f` has published, so it never sees that Run half written.
What can change under it is the Source's *next* Run, which Pond Runs pipelining allows while the Sink is still
running.

None of the items here moves the window or the as-of bound, so no run reads data of a different freshness than
today, and `changed_f`, pins and demand are untouched. The fast path must keep `apply_zset`'s replay rule
(`DELETE` at `f` then `INSERT`) and its "changed" return. What some items do change is *when* files are read:

| Item | Effect on reads | Safe as it stands |
|---|---|---|
| 1.1 skip unused current state | fewer reads, decided at compile | yes |
| 1.2 one-pass joins | reads the same `δL` and `R'`, once | yes |
| 1.3 one consolidation | computation only | yes (the "already consolidated" flag is internal) |
| 1.4 lazy chain delta | the Pond's own registry, same Ripple | yes |
| 1.5 footer counts | metadata of the same parts | yes, hot parts only (bands aren't `f`-homogeneous) |
| 1.6 chunked parts | a run becomes several files | no: see 1.6 |
| 2 detection | footers of the parts in the window | yes, if taken from the same file list as the delta read |
| 2 propagation, skipped consolidations | computation only | yes |
| 2 reading parts in place | later reads | no: needs Stage 0 |
| 2 writing once | the change log becomes files written by the Ripple | no: needs Stage 0 |

### Existing hazards (found reading the code for this plan, not reproduced)

- **A Sink can read the wrong current state after its Source compacts.** Nothing holds a Source's warm fold or
  cold compaction back while a Sink run is in flight (`Driver._retain_from` covers plain-table versions only). A
  cold compaction at `f2` writes a base folded up to `f2` and removes the old base's chunks. A Sink run at
  `f1 < f2` that reads after it reconstructs as of `f1` from the new base filtered to `_duckstring_f <= f1`: a
  row last written in `(f1, f2]` has lost its earlier image in the fold, so it's missing, silently. The
  checkpoint's own note ("overlap-safe", `_publish_base_chunks`) reasons about latest-state readers, not as-of
  ones. The delta side degrades safely (the fold raises the floor, so the Sink misses coverage and reads in full).
- **A Draw can ship part of a run.** It ships every part newer than the consumer's `after` with no upper bound,
  and the consumer's `landed_after` is the newest part it holds. Since 4bcafa2e (2026-10-24) an append history
  run can be several chunks, moved into place one at a time; a Draw taken mid-move ships some of them, and the
  consumer never asks for the rest. The sidecar can't act as the commit marker either: `publish_plan` writes it
  before the parts.

### Stage 0: read snapshots

Make a Sink run's reads of a Source name exact, immutable files, and keep those files until the run is done:

- Resolve each Source read to an explicit file list at compile (the parts in the window, the bands and the base
  chunks for the as-of state), instead of a glob evaluated per query. Names are already unique: parts by `f`,
  bands by range, base chunks by checkpoint token.
- Choose the base an as-of read uses like an overwrite version: the newest checkpoint token at or below `f`,
  plus the bands and parts above it. A reader at `f1` after a compaction at `f2` then reads the old base.
- Never delete a merge tier's file that an in-flight Sink run may need: extend `retain_from` to folded hot
  parts, superseded bands and superseded base chunks, and delete them at a later publish or the reap's
  `shutdown` once `retain_from` has passed. Persist's S3 pruning follows the same bound.
- A Draw ships only parts at or below the Source's committed freshness, read from a marker written after the
  parts (the Catchment's `end_f`, or a sidecar written last).

Tests: a Sink run held mid-read while its Source folds and compacts reads exactly the state a recompute as of
its `f` gives; a Draw racing a chunked publish lands every chunk.

## Expected effect

A guess from the SF100 profile, to be measured: at SF1000 the new-sales join step from about 10 s to 2 to 3 s,
`customer_value` from 9 s to 1 to 2 s, so the change from 21.7 s to roughly 8 to 10 s, with ingest a real share of
what remains. The fact and mixed cases gain from Part 1 and less from Part 2.

## Tests

The general path stays the oracle.

- `packages/trickle/tests/test_trickle_join_delta.py`: insert-only random changes through every `how`, chain and
  bushy shapes; the fast path's output, consolidated, equals the general path's and a recompute.
- A run with one update flips exactly the nodes the table above says, and the next insert-only run is fast again.
- The key-collision rule: a non-pass-through key with an insert that collides takes the checked path and is
  correct.
- Footer detection: a part with a `-1`, a warm band, a missing statistic (treated as not insert-only), a window
  spanning a fold.
- Part 1 items each keep `test_trickle_join_delta.py`, the aggregate suites and the three SF100 chain cases
  verified against DuckDB's rebuild (`bench/tpcds_chain/verify.py`).

## Stages

0. Fix the two existing hazards and add read snapshots (Stage 0). Needed before anything that reads later or
   splits a run into files, and worth having on its own.
1. Part 1, items 1 to 4 (lazy current state, the one-pass unchanged-side joins, one consolidation, lazy chain).
   Measure the three SF100 cases.
2. Detection and propagation, the skipped root and `apply_zset` consolidations, insert-only reads downstream,
   Part 1 item 5, and item 6 as a single-file write. Measure.
3. Writing once. Measure.
4. Splitting mixed changes, if the fact case still warrants it.

## As built (2026-10-25)

Stages 0 to 2 are built (f1aa90d4, cd9d0b08, 62c2250f). What differs from the plan above:

- **Stage 0** follows the plan. The sidecar is written last; reads name explicit files (`merge_snapshot`,
  `parts_snapshot`); `gc_tiers` collects superseded files after the commit, at the reap and after a Draw lands.
  `tests/test_read_snapshots.py` reproduces both hazards (they fail on the code before it).
- **Item 1.1** builds a Source's current state the first time a term reads it (columns come from a binding,
  `Pond.source_columns`), and an aggregate builds its current state only when a group rescans. Stage 0 makes the
  later read safe.
- **Item 1.3** consolidates once, after the pipeline, and only for an aggregate or a scan: `apply_zset` and
  `append_zset` consolidate what they write anyway.
- **Detection** reads `min(_duckstring_d)` over the window's rows (one column of the change) rather than Parquet
  footers: simpler, and cheap next to the window it guards.
- **The key-collision check** wasn't needed. Consolidation only nets identical rows; it never checked a `+1` for a
  key already present, so writing an insert-only change unconsolidated is the same Z-set as before.
- **Item 1.6 and Stage 3 are dropped.** A single-file write of SF1000's 13.4M-row change takes 0.4 s on the
  laptop whatever the options, so chunking the change log would gain nothing. The publish's time is elsewhere:
  `_export_parts` scans the registry change log (`DISTINCT f`, then `WHERE f =`) to find the run's rows, and an
  aggregate's Extension-1 state snapshot rewrites every group each run (`_export_companions`).
- **Item 1.5** (footer counts) is left: the `p` check costs 0.05 to 0.1 s at SF100.

Cooled SF100 changes on the laptop (each after 150 s idle; every output checked against DuckDB's rebuild, which
takes 25.7 s cooled):

| Change | before (1296df96) | after (62c2250f) |
|---|---|---|
| New sales | 4.17 s | 2.21 s |
| Fact | 5.58 s | 4.59 s |
| Mixed | 8.91 s | 7.82 s |

Next, for the new-sales case at SF1000 (est. 2.2 s and 1.4 s there): publish a run's change log part from the
change the write already holds, and snapshot only an aggregate's changed groups.
