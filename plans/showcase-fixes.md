# Fixes found by the transit showcase

Status: **proposed 2026-11-13; being built on `showcase-fixes` (2026-11-15).** Done: 0, 2, 3, 4b, 5, 6 (the race;
the pause is the design, below), 7, 8, 16, and from 9 the puddle viewer, `Cache-Control` and log noise; also
attention FINDINGS 3 and 14, and the root cause behind 16 (Trickle's scratch views and `read_table`'s Source view
were stored in the registry). 1 is redesigned in `plans/source-read-positions.md`. 4a and 4c wait for a table that
needs them (the benchmark's remaining slope is part listings; folding by count would make slow consumers of a fast
Source read it in full). From a showcase pipeline run on released 0.8.0 (duckstring-trickle
0.3.1, DuckDB 2.0.0, macOS, a local Catchment): Melbourne's train feeds polled every 31 s through two Inlets, three
Trickle Ponds and an App, 211 cycles and 1,266 Pond Runs. Its tables are small (tens to a few thousand rows per Run)
and its Runs frequent, which no earlier test combined, so most of what it found is about many small Runs. The
showcase's own notes hold the reproductions; this plan holds what to change. Code references are to `main` at
`1ff8a711`.

Priority order: 1 to 4 can fail or slow a pipeline left alone; 5 to 7 are cost and ergonomics; 8 and 9 are small.
Items 10 to 13 (added 2026-11-14, from the attention showcase) are about iterating: 10 and 12 matter most. 14 (Inlet
lag) is a reporting feature; 15 is a note to create a skill. 16 (added 2026-11-15) is a bug: concurrent reads
in an App fail, which caps a serving App at one reader.

## 0. A many-small-Runs benchmark first

The showcase measured Runs slowing with their count (median per block of 20 Runs, 0.055 to 0.129 s for the builder
`.append()` Pond, 0.087 to 0.207 s for the incremental-aggregate Pond, linear over 210 Runs), but didn't separate the
causes: empty marker parts (2), un-compacted append parts (4), the fast path's history reads (5), the sidecar's size.
Before fixing 2, 4 and 5, add `bench/small_runs/`: an append Inlet of ~200 rows per Run, a builder `.append()` join to
a merge dimension that rarely changes, an incremental aggregate, 2,000 Runs through `trickle.at` windows and through
the data plane. Report Run time and file count per 100 Runs, then re-run it after each fix. Gate: Run time flat
(within noise) from Run 100 to 2,000.

## 1. A window on an Inlet hides its Trickle tables from Sinks with other Sources

**Silent wrong data.** A windowed Inlet's Run is stamped with the window's end (`engine/catchment.pond_source_f`), so
its Trickle rows carry `_duckstring_f` in the future. A Sink with another, unwindowed Source runs at that Source's
freshness (now) and reads Trickles `as_of=self.f` (`core.py` 1265, 1284, 1328, 1357), so the windowed Source's rows
are invisible until the window ends. In the showcase, a 6-hour window on the timetable Inlet made a builder join see
an empty timetable; the output was an append table that freezes rows, so 2,568 observations stayed "unscheduled"
with every Run succeeding. Plain tables are unaffected (they read by `pin`), and so is a Sink whose only Source is
the windowed Inlet.

Options:
- **A. Stamp rows with the Run's start, keep the window's end as the Pond's freshness.** The Duck gets a write stamp
  separate from `F`: `pond.f` (and every Trickle write, floor and `f_boot`) is the Run's start, minted once and kept
  in the ledger so a replay reuses it; `end_f` stays the window's end. A Sink at `f` then sees rows written at or
  before `f`, and a later Sink window `(f, f']` sees the next window's rows once. Touches the Duck, `BeginRun`, the
  ledger, and the run stamp `_duckstring_run` (which should then be the write stamp).
- **B. Express a window as staleness allowance, not freshness.** `pond_source_f` returns the Run's start with `D`
  stretched to the window's end. Engine-level, so a Playground change and a cross-check; it changes what freshness
  means for every windowed Pond and Spout.

**Decided 2026-11-15: neither.** Read each Source over its own window, from the previous Run's read position to
this Run's (`plans/source-read-positions.md`). A only fixes the windowed case: a Sink still leaves out the rows of
a Source Run that ran after its own freshness, the Run that enabled it.

Superseded recommendation: A. It keeps the engine's meaning of a window ("fresh until the window's end") and fixes
only where the rows land. Until it ships, refuse or warn at `window add` on an Inlet whose Trickle tables a Sink with other
Sources reads, and say so in the Windows guide.

Fixing this also answers the showcase's "nothing between every Refresh and a window" (a Refresh on an Outlet demands
a rarely changing Inlet every period; a window is the idiom to limit it, and it was unusable). Document the window as
that answer once A lands.

Tests: an engine-plus-Duck e2e with Inlets `a` (merge table, 6 h window) and `b` (none), Pond `c` joining both; `c`
sees `a`'s rows on its first Run and exactly once across windows. Replay at the same Run keeps the stamp.

## 2. Skipped Runs publish files: empty marker parts and full plain-table copies

Two faults, both on Runs where the Pond called `pond.skip()`.

**2a. A new empty marker part per Run.** `dataplane._export_parts` (line 879) writes a 0-row part at the Run's `f`
whenever the registry's change log has no rows at all, so a bootstrap-only table reads as an empty delta rather than
a coverage miss. The marker is named after the current Run, so a merge table bootstrapped once and never changed
gains one per Run: the timetable's four tables had 359 parts each after three hours, ~11,000 files a day, all read by
every reconstruction downstream.

Change: write the marker only when the table's part directory holds no part (move the `existing` listing before the
`reg_fs` fallback). Then check whether the marker is needed at all: the sidecar already records each table's schema,
and `read_select` already builds an empty relation with the table's columns when nothing is at the read's freshness
(line 823); if coverage holds without it (the floor is in the sidecar), drop it and keep `gc_tiers` from expecting it
(the comment at line 402).

**2b. Every plain table republished, and the copies kept.** `ParquetDataPlane.export` writes a new version of every
plain table not in `held`, written this Run or not. On a skipped Run nothing is held, and `Driver._retain_from` keeps
every version from the line's `changed_f`, which a skip holds at the last real change. The showcase's one-row
`feed_version` had 360 identical versions after three hours; a large plain table on a mostly-skipping Pond would be a
full copy per Run, kept until the next real change.

Change: a skipped Run (`DuckCore._skipped`) exports with every plain table held and writes no Trickle part, so its
publish is the run stamp alone. `pond.skip()` already asserts the output didn't change, so holding everything is that
claim taken at its word.

Open question for the author: a normal Run also republishes plain tables it didn't write (retention bounds that, but
each Run still copies them). Holding every table the `ChangeLedger` didn't see written would stop it, but raw SQL on
`pond.con` is invisible to the ledger, so a table written that way would silently stop publishing. Leave as is, or
hold unwritten tables only when every Ripple of the Pond writes through the handle?

Tests: 2,000 skipped Runs leave one marker per table and one version per plain table; a skipped Run after a real one
leaves the published state byte-identical apart from the run stamp.

## 3. Parallel Ripples race on a Pond's first Run

The Ripples of a Pond are threads in one Duck sharing one DuckDB instance (`duck/executor.py` opens the registry once
and gives each Ripple a `.cursor()`), so they are concurrent transactions on one catalog. On a first Run each Trickle
write calls `trickle.io._ensure_meta`, `CREATE TABLE IF NOT EXISTS _duckstring_trickle`, and two Ripples committing it
together fail with `Catalog write-write conflict on create`. It failed the five-Ripple Pond in 3 of 6 offline runs. With
both retry budgets at their default 0, the Pond stays failed until someone clears it. `core.retry_on_lock` covers lock
errors, not this.

Change: create the shared system tables once when the executor opens or hydrates a registry, before any Ripple runs:
the Trickle meta table (a public `trickle.io.ensure_meta`, since the package must not import Duckstring; the executor
calls it), and any other table more than one Ripple may create (`_duckstring_fp`, `core.INPUTS_TABLE`; audit for
`CREATE TABLE IF NOT EXISTS` reached from Ripple code paths). `_ensure_meta`'s `ALTER TABLE` migrations then run once
per Duck too. As a second line, treat a catalog write-write conflict like a lock error in the executor (one immediate
retry of the Ripple, not counted against `immediate_retries`).

`immediate_retries` now defaults to 1 (the author's call, 2026-11-18; `routes/deploy.DEFAULT_IMMEDIATE_RETRIES`):
a first-run race, a transient storage error or an OOM retry are all cheaper retried than left failed for a person.
It seeds a line's budget at its first deploy, so lines already deployed keep theirs.

Test: a Pond of eight Ripples each writing a merge table, first Run, repeated 50 times on a fresh registry: no failure.

## 4. Append tables are never compacted, and merge tables only by bytes

Every Run adds a part per Trickle table. Merge tables fold when the change log passes twice
`DUCKSTRING_COMPACT_THRESHOLD` (256 MiB); append history, `__droplog`, and an append table's own parts are never
folded. At ~2 KB per part the showcase's aggregates would fold after ~250,000 Runs. The per-table
`compact_threshold=` exists on `Pond.merge_table` but not on the builder's `.merge()` terminal, so builder outputs can
only be tuned through the Catchment-wide variable (which also sets base chunk size).

Changes:
- **Fold by part count as well as bytes.** `fold_warm` also triggers when the hot tier holds more than N parts
  (say 64; a constant with an env override), leaving the hot window it leaves now.
- **`compact_threshold=` on the builder's `.merge()`**, passed through to `apply_zset`/`merge_table`'s meta record.
- **Append bands.** Fold an append table's parts older than `retain_from` into range parts, the counterpart of merge
  bands: rows keep their `_duckstring_f` (reads are content predicates), the file is named by its range so
  `parts_snapshot`, `gc_tiers`, the Persist mirror (reconciles by name), Draws (`part_f > after`, then
  `landed_after` from names) and `_export_parts`' reconciliation all see it. A Draw must replace the parts a range
  covers on the consumer, as a base does now. Same trigger: part count or bytes.

Do the bench (0) first: if the growth is mostly 2a and 5, append bands can wait for a table that needs them.

Tests: `test_read_snapshots`-style reads across a fold (a Sink reading as of a freshness inside a folded range gets
the same rows); a Draw across a fold; the Persist mirror after a fold.

## 5. The builder's append fast path reads the whole output history

`TrickleBuilder._new_spine_rows` (`builder.py` 750) keeps the spine delta's rows whose key isn't in the output
(`WHERE (pk) NOT IN (SELECT pk FROM out WHERE _duckstring_f < f)`), and `trickle.io.append_zset` then joins the batch
against the whole history again for its conflict check. Both are O(output history); the join itself is O(new rows).
In the showcase the output grows ~200,000 rows a day.

Change: when the spine is an append Trickle with a declared pk and the read is a delta (not `is_full`), every key in
the delta is new to the spine's history (an append table's stored history is pk-unique: conflicts raise or are
dropped), and the output's key is the spine's key passed through verbatim (the fast path's own precondition), so
every row is new to the output except a replay at the same `f`, which the append write already replaces. Skip both
history reads in that case. Otherwise keep them.

Test: extend `test_trickle_join_delta`'s random harness with append spines under replays, coverage misses and
dimension changes, comparing against a recompute; assert by a query counter that the fast path reads no history.

## 6. Restarting the Catchment can fail an App

Stopping the Catchment (SIGTERM to `catchment start`) failed the App on one restart of three: the restarted
Catchment showed it failed with `the App's process exited (exit code -15)`, and its budget of 0 was spent.

Likely cause, to confirm: a race in `AppSupervisor`. `reconcile` (a thread, every second) checks `inst.stopping`, then
`inst.proc.poll()`. `shutdown` → `_stop(inst, "shutdown")` sets `stopping` and SIGTERMs the process group. If
`reconcile` passed its `stopping` check before `shutdown` set it, it sees the process gone, records `exited` and calls
`_exited`, which runs `Driver.fail_app` and persists the failure.

Change: in `reconcile`, re-check `inst.stopping` after `poll()` and skip `_exited` when it's set; stop the supervisor
loop before `shutdown` stops instances; `_exited` never fails an App while the Catchment is shutting down.

Explained 2026-11-15: a failed Pond counts as blocked and refuses demand, so a recurring Refresh on a failed App
reaches nothing upstream until the App is cleared. Kept, by decision: an upstream that should keep running needs
its own demand; one that runs only for what uses it correctly stops when its user is broken. Worth a line in the
Apps guide.

Originally not explained: after that restart the chain's next Run came 4 min 23 s after the Catchment was back (under a minute
on the other restarts), while the recurring Refresh was on the failed App. Check how a failed App's Refresh demand is
treated, and whether a failed App should hold back its Sources at all (it never runs anything).

Test: SIGTERM a Catchment with a running App 50 times at random offsets; the App is never failed on restart.

## 7. Builder `.filter()`/`.mutate()` need `alias.col` even with one source

`pond.trickle("x.t").filter("col")` fails with DuckDB's `Referenced column "col" not found in FROM clause!`;
`.alias("e").filter("e.col")` works, and `.aggregate()` takes bare names. Change: in `_qualify`, resolve a bare name
that exactly one leaf has (as `on` does through `_resolve_col`), and raise a `BuildError` naming the rule when it's
ambiguous or absent. Test in `packages/trickle/tests/`.

## 8. An App's freshness can't tell "ran" from "changed"

`app.pond().freshness()` moves on every Source Run, passes included, so an App rebuilding on freshness rebuilds every
cycle. `GET /api/ponds/{name}/freshness` already returns `changed`; give `AppPond.freshness()` each Source's
`changed_f` as well (from the run stamp and entries it already reads, `AppPond._stamps`).

## 9. Smaller

- **`pond run` and an App's `static`.** `local/app.py` runs the `command` only when both are set, so every static
  path is a 404 locally while a Catchment serves them. Serve `static` locally the way `app_proxy` does.
- **The puddle viewer misses Trickles.** `cli/puddle.py:_inventory` globs `*.parquet` and `__v/` only. List append
  and merge directories from the sidecar and query them through `read_select`.
- **`pond run` can't replay incrementally.** A downstream local run always has `previous_f = None`, so local runs
  only test the first-run path of every builder. Now part of item 10 (`pond run --incremental`).
- **No `Cache-Control` on an App's static files** (`app_proxy.py` 140, `FileResponse`). Send `no-cache` (revalidate
  by ETag) by default.
- **Done 2026-11-18, per line:** a line first deployed from 0.9 on publishes 128-bit integers as `BIGINT` (migration
  `037`); agg/acc state is published exactly as `DECIMAL(38,0)` on every line.
  **`agg.sum` over an integer column returns DOUBLE.** Keep an integer type (BIGINT, or DECIMAL(38,0) where it could
  overflow, the HUGEINT question from 0.7.0).
- **Docs.** `agg.argmax` returns the latest row's value with NULL included, unlike DuckDB's `arg_max`, which skips
  NULLs (a plain-SQL check written with `arg_max` disagrees); say so in the `agg` reference. In the ingesting guide,
  note that DuckDB 2.0 refuses a view over an Arrow relation in a file registry and `con.register(name, table)` is
  the way to name one for SQL.
- **Log noise.** `flock: engine 'starrocks' could not be checked (No module named 'sqlglot')` repeats during Runs with
  no Flock configured; check engines only when the mode isn't `off`, and log once per process. `no published base at
  or below 0001-01-01` on first reads should be debug.
- **Memory for small Ponds.** Five Ducks held ~1.4 GB between them for tables totalling a few MB, plus 489 MB of Pond
  environments for two Ponds. Not a bug; worth a measurement against the sleeping-VM hosting idea (idle DuckDB memory
  per Duck, and whether small Ponds could share a Duck).

# From the attention showcase (2026-11-14)

A second showcase, "the world's attention" (`private/duckstring-cloud/showcase/attention/`): twelve Ponds joining
hourly Wikipedia page views (about 4M rows an hour) to Wikidata's 88M sitelinks, GDELT news and Bluesky, plus a
co-mention graph and a two-year history. Its `FINDINGS.md` has the details. Items 10 to 13 are what most slowed
iterating on it; 14 and 15 came out of reviewing it.

## 10. Live Puddles: a local run against real Source data

Every metric in the showcase was designed off-platform: ad-hoc scripts reading the published Parquet with
`dataplane.read_select`, then ported into a Ripple. For `wa_topics` that went as far as a harness with a stand-in
Pond handle so the real Ripple could run against live data. Puddles didn't serve:

- The Puddles chained local `pond run` outputs, so each Pond's local data was a different, small window (1.25 hours
  of news against 2 hours of page views). The code ran, but the numbers meant nothing; metric design needed the real
  24 to 48 hours.
- A real-sized snapshot is gigabytes (`wa_views` 3.2 GB published, the sitelinks 2.3 GB). Local Catchment: a copy of
  what's already on disk. Remote Catchment: a download per refresh.

Decided (2026-11-19): **live and replayable Puddles**, not a `pond try` that runs candidate code on the Catchment. A
try line works for one Pond but a change spanning two Ponds becomes a dev subgraph on the production Catchment. A
Puddle is already "a Source, as a local run sees it", so it can be a route to the data as easily as a copy of it, and
`pond run` stays the one way to run code before deploying. Tiered dev/qa/prod Catchments (dev reads qa and prod, prod
reads nothing) are the answer for testing orchestration itself, and a Cloud feature, outside this item. Two rules from
the discussion: nothing about production changes because of what is testing against it (no read holds, no retention
bounds), and hydration is the active step (no mode is ever a setting in `pond.toml`). Keep the first cut simple.

### Reading Sources in place

**`p.source(table)`** on the `Puddle` handle: a DuckDB relation over the Source's published table, read in place (not
through `/api/query`). The Source's major is the one its `[sources]` pin selects. Where the data is: a registered
local Catchment's `root` (`registry.resolve_data_dir`, the local publish), else its data root (the registration's
`data_root`, else the Catchment's settings) read with the user's own credentials, as a remote Duck would. A Catchment
whose data the CLI can't reach (a hosted Catchment on local disk, a Volume through the Files API) raises, naming
`--from-catchment` and `p.catchment()` as the way round. A filter written with `p.source` reads only what it needs in
plain hydration too.

**Write methods matching the Pond's.** The handle gains `p.append_table`, `p.merge_table` and the Pond's form of
`p.write_table`, so a definition (baseline or step) writes the shape the Source has, append, merge or plain, with
the layout incremental reads need. The Trickle demo's Puddles (`demo/priced/src/puddles.py`, `demo/revenue`) build that
layout by hand through the data plane today; they move to these.

### Live Puddles

**`pond hydrate --live`** (with `--source` for some Sources only) marks Sources live in `puddles/hydrated.json`
instead of copying them.

- **A Source with no definition** is the whole Source, live.
- **A definition that uses `p.source`** isn't run at hydrate. `pond run` runs it at the start of each run, and the
  relations it returns or passes to `write_table` become what the run reads for that table, as SQL over the pinned
  files (`relation.sql_query()`; the data plane's reads name explicit files, so it is self-contained). A filter or
  projection stays, and DuckDB applies it in the scan. The handle records whether `p.source` was called, so this
  needs no annotation. In an incremental run the definition is evaluated at the previous and the new position, and
  the difference is the window's change, so it must be deterministic (as for a copied history).
- **Any other definition** (generated rows as in the demo, a CSV, `write_path`, `write_object`, `p.catchment()`) has
  no live data behind it: it is copied at hydrate as now, and `--live` says so per Source.

A live hydration records its time (or `--as-at T`) in `hydrated.json`, and a baseline `pond run` reads each live
Source as it stood then: the version at or below its freshness at that time, Trickle rows up to it
(`read_select(pin=, as_of=)`). That keeps a live Puddle close to stable across runs without copying it; hydrating
again moves it forward. The versions read are recorded in `puddles/out/` beside the outputs, so a result says what it
was computed from. **No read hold**: what production prunes is gone. A plain table's version at the hydration time
is usually gone once its line publishes again (the newest is read, with a warning, as a deployed read does); a merge table
read before its last compaction, or a version a long run is reading, fails with a message saying so. When stability
must be guaranteed, copy instead (`--as-at` without `--live`).

### Replayable Puddles and incremental runs

The purpose of an incremental local run is to test that the incremental logic works, which needs Source data at
epochs after the baseline. Live and copied Puddles both give it: a live Source replays from its recorded hydration
time through what production has published since (its history grows as production runs, so a live test can follow
production forward), a copied one through the history copied with it.

**`pond hydrate --as-at T`** copies each Source's state at `T` plus every change recorded since, and the Source's run
freshnesses from `/api/runs` (passes included); a live Source's are read from `/api/runs` at each run instead (a
local Catchment that isn't running falls back to the Source's change points, with a warning). A Source's freshness
advances on every run, changing or not: a dimension that runs hourly but changes weekly reaches a freshness within
the hour, which its changes alone would not show. Without a definition, the files are copied (what a read at `T` picks, `merge_snapshot`/`parts_snapshot`, plus
everything newer). With one, the definition is evaluated at `T` and at each of its Source's change points after it,
and written as the Source's kind of table, so the copy holds only the differences and a filtered history stays
small. A definition must be deterministic for this (a filter, a join, a sample by key hash; not random sampling).
A `T` older than a Source keeps (a merge table before its last compaction, an append table below its floor) is
refused, naming the earliest `T` allowed. A plain table usually has only its newest version: if the version at `T` is
gone, the newest is used and hydrate says so. Without `--as-at`, the baseline is the Sources' current state and there
is no history to replay.

**`@puddle.step(target, set=...)`**: a definition that writes the data for the next window of a copied Puddle,
through the same write methods. It sees `p.previous_f`, `p.f` and `p.step` (the step number, so randomness can be
seeded per step and a rerun from the baseline writes the same rows). Only for copied Puddles: a step writes into the
local copy, never on top of live data. A step has no cadence of its own.

**`pond run`** (no flag) is the baseline: it resets `puddles/out/`, drops what steps wrote (back to the hydrated
copy), and runs with `previous_f = NEVER`. **`pond run --incremental`** keeps the registry and advances to a target
freshness, emulating a Refresh: the Pond runs only once every Source has reached the target.

- **Targets**: `--next-change` (the default): the next point after the last run's freshness where any Source changed.
  `--next-run`: the next freshness this Pond ran at in production, from its own run history (recorded at hydrate
  for a copy; a deployed Pond only), so it replays exactly the windows production saw. `--most-recent`: the oldest
  of the Sources' latest recorded freshnesses (for a live Source, its current one). `--to T` is what the three compute, and can be given directly. No `now`: historical
  Sources can't update to it.
- **Reaching the target**: each Source is read as it stood at the moment the last of them reached the target (its
  latest recorded freshness at or before that moment). A copied Source whose history doesn't reach the target steps,
  if it has a step; one with neither, or a live Source production hasn't brought there, refuses the run, naming the
  Source.
- **A step's stamp** is the target freshness while any Source still has copied history, and now once none has. So a
  wholly synthetic test is always now, and a mixed one stays in step (stamped now, a step's rows would sit past the
  run's freshness until every other Source caught up).
- **The run** is stamped as the engine would stamp it, the oldest of its Sources' freshnesses; rows past that wait
  for the next run. `previous_f` is the last local run's freshness, so Ripples read the real window.

The local-testing guide should note that moving from the end of copied history to now is a jump in time: logic
relative to `pond.f` ("the last 24 hours") and `retain_t` see it. Synthetic rows can carry timestamps within the
step's window.

### Named sets

`@puddle(target, set="recent")` and `@puddle.step(target, set=...)`, or `set=("recent", "full")` for a definition in
several. Unlabelled definitions are the default set. `pond hydrate --set recent` uses, per Source and table, the set's
definition where it has one and the default otherwise; the chosen set is recorded in `hydrated.json`. `--set`,
`--live` and `--as-at` are independent. Two definitions for one target in one set are an error at collection; before,
both ran and the last write won silently.

`pond puddle ls` shows each Source's mode (live, copied, synthetic), its set, the copied range and the last run's
position.

**Docs** (made with the code, not part of the held pass): the `pond hydrate` and `pond run` CLI reference, the
`Puddle` and `@puddle` Python reference with their docstrings, and the local-testing guide.

**Tests**: `p.source` reads from a local Catchment root and from an object-store root (moto); a filter definition stays
a filter under `--live`; a generated definition is copied under `--live`; a live baseline reads the Sources as of
the hydration time, and `--incremental` replays production's changes since through a filter definition; a pruned
plain version falls back to the newest with a warning; `--as-at` copies history and run freshnesses, and refuses a `T` before
a compaction; `--next-change` waits for every Source (an unchanging, regularly running Source doesn't hold it back);
`--next-run` replays production's windows; `--most-recent`; a step fills a Source past its history, stamped at the
target, and at now once nothing has history; a Source with neither refuses; a plain `pond run` drops step output; the
builder and an incremental aggregate give the same result replayed step by step as a baseline at the end; set
selection with fallback; a duplicate in one set raises; an unreachable data root names the fallback.

**Follow-up, to agree before building**: a Source resolved to a sibling project's local output, for a change spanning
several Ponds.

## 11. Filling a new table or Ripple from data already loaded

Adding `article_place` to the `wa_news` Inlet (attention FINDINGS 11) had no way to fill its past short of a
Rebuild. That would reload 48 hours of files and re-bootstrap everything downstream. The Ripple instead keeps its
own `place_file` log and refetches the old files itself. Two changes:

- **A Ripple with no completed Run reads its Sources in full.** Check what a Ripple added to an existing Pond reads
  today. `DuckCore.previous_f_for` moves `previous_f` up to the Ripple's own last completed freshness. A Ripple with
  none should get `NEVER`, so its first Run bootstraps from everything its Sources hold, not from the Pond's
  `end_f`.
- **A per-Ripple Rebuild.** `control rebuild POND --ripple NAME` drops only that Ripple's tables and re-bootstraps
  it (raising floors only on its outputs), leaving the Pond's other Ripples and its downstream untouched except
  through that table. For an Inlet Ripple the engine can't replay the outside world. Document the log-of-inputs
  pattern `place_file` uses, or give Inlet Ripples an optional `backfill(pond, since)` hook that a per-Ripple
  Rebuild calls.

## 12. A CLI that works for agents

In both showcases I wrote throwaway scripts against `/api/runs` and `/api/status` in every session, and one
`status` call hung for its full timeout. Most users will be agents, so the defaults should suit them:

- **Live views only on a terminal.** `status`, `refresh now`, `wake` and `force` open a rich Live view. When stdout
  isn't a TTY, `status` should print one snapshot, and the one-shot commands should wait quietly and print the
  outcome. `--live` forces the view; `--once` and `--silent` stay as they are.
- **Waiting with exit codes.** `--wait [--timeout D]` on `refresh now`, `control rebuild|wake|force` and `pond deploy`
  (until the new version has completed a Run). Exit 0 when the target and the Runs it caused succeeded. Otherwise
  exit non-zero and name the failing Pond, Ripple and error. This replaces every polling loop I wrote.
- **`duckstring runs [POND] [--ripples] [--limit N] [--json]`.** Version, `f`, status, start, finish and duration
  per Run, and per Ripple with retries and errors. Run history isn't in the CLI today; it was the script I rewrote
  most.
- **`--json` on `status`, `runs`, `refresh ls` and `data ls`,** with the `/api/status` fields an agent needs and
  the table hides: version running, last Run's per-Ripple durations, `rebuild_pending`, queued Runs, memory slots
  held, last error.

## 13. Use as few Ponds as the pipeline needs

For exploration, one Pond with several Ripples should feel like a DuckDB script. A Pond boundary should be there
because the pipeline needs it, now or in a future that is already known. The showcase split into twelve Ponds
partly for good reasons and partly by force:

- **Good reasons:** different cadences (hourly, weekly, every 15 minutes), failure isolation (a Bluesky disconnect
  shouldn't stall page views), memory per Duck, and reuse (the names map feeds two Ponds).
- **By force:** a builder can't start from a table another Ripple of the same Pond wrote (attention FINDINGS 2), so
  `chatter_hour` had to move to the next Pond.

**Code done 2026-11-15** (`pond.trickle("own_table")` and `pond.read_delta("own_table")`, with an own Trickle read
as of the Run's freshness so a writer pipelined ahead doesn't leak; reference docs updated). The Concepts and guide
text below is still to write, held for the docs pass.

Fix the force: allow `pond.trickle("own_table")` when its writer is an ancestor Ripple. Read its in-run delta from
the registry (`trickle_io.read_registry_delta`, as a mid-chain `.merge()` already does) over the reading Ripple's
own `previous_f`. Then say in the docs (Concepts, and the guide on structuring a pipeline): use as few Ponds as the
pipeline needs now or in a known future; split when cadence, failure isolation, compute size or reuse calls for it.

## 14. Inlet lag: how far an Inlet's data trails its freshness

Freshness keeps its meaning: the time of the Refresh that drove the Inlets (for a windowed Inlet, the window's end).
What it can't say is how old the data itself is. Wikimedia publishes an hour's page views one to three hours late,
so the showcase's page read "fresh as of 20:11" while its newest page views were for 17:00. That depends on the
source, so only the Inlet can know it.

- **Reporting.** An Inlet Ripple reports a lag, `pond.set_lag(timedelta)`, measured from the window's start (from
  `pond.f` without a window). A windowed Inlet's data can be as old as its window's start, so its lag is the window
  width plus what it reports; with no report, the width alone, and zero for an Inlet without windows.
- **Open point: the reference.** An Inlet that works its lag out from its data (`pond.f - max(hour)`) would count the
  window width twice. Either the handle gives the reference (`pond.window_start`, equal to `pond.f` without a window),
  or the call also takes a timestamp, `pond.set_lag(as_of=t)`, from which the engine derives the lag.
- **Stored as a time.** Keep the time the data describes, `F - lag`, on the `pond_run` row. A skipped Inlet Run
  (no report) carries the previous one forward, so while the source publishes nothing the lag grows as freshness
  advances, which is true. An Inlet whose source is current but quiet reports on its skip too.
- **Downstream.** A Pond's data time is the minimum, over its root Inlets at the versions its Run pinned
  (`source_pins`), of each Inlet's data time; its lag is its freshness minus that. Computed by the Driver at Run
  completion, so a Ripple has nothing to do.
- **Reported only.** Lag never enters demand, staleness, the throttle, windows or `d`, so orchestration can't change.
- **Surfaces.** `/api/status` and `/freshness` (`lag`, `data_as_of`), the App handle's `freshness()`,
  `duckstring_pond_data_lag_seconds` on `/metrics`, the UI's age display and timing chart, and an alert bound on data
  age beside `--stale`.

## 15. A Duckstring skill for agents

Create a skill that agents load when writing and running Ponds.

## 16. Concurrent merge-table reads in an App

Two threads reading the same merge-table Source through `duckstring.app.pond()` at once fail on one of them:
the changelog consolidation's temporary table (`trickle.io.materialise_reconstruct`, named by `unique_name`)
is missing when the other thread's query binds (`_duckstring_ds_consol_0 does not exist! Did you mean
"_duckstring_ds_consol_1"?`). The handle gives each thread a cursor of one connection (`AppPond.con`), and the
read and its view evidently don't stay on one of them. Reproduce with two threads each running
`read_table("source.merge_table")` then `SELECT count(*)` a few times (showcase attention FINDINGS 16). Fix: keep
every temporary object a read creates on the calling thread's cursor, and add a concurrent-read test for the App
handle.

## 17. A fixed Pond's root Ripples stay running after the redeploy (found 2026-11-15)

**Fixed 2026-11-15** as guessed below: the ledger's Ripple rows record the version that wrote them, and a Duck
seeds only its own version's (`engine/pond.load_state`), so a new version runs every Ripple of a re-dispatched
Run, as the Catchment expects. The reproduction now ends with every Ripple idle, each run once.

Found while trying `--wait` (item 12) on a throwaway Catchment with real Ducks, and reproduced on `main` (0.8.0),
so it predates this branch. The demo pipeline: deploy, `refresh now reports`; deploy `sales` 1.0.1 whose
`join_lines` raises, `refresh now reports` (it fails); deploy the fix as 1.0.2. Fifteen seconds later `sales`
reports `running` with `start_f == end_f` and no Run in flight: its root Ripples (`daily_sales`, `price_tiers`) are
`running` in the engine, `join_lines` `queued`. A later Refresh does run the pipeline, and leaves the roots
running again afterwards. So the UI and `/api/status` show the Pond running for good, and `--wait` on it never
settles.

Likely mechanism, to confirm: `clear_on_redeploy` → `clear_pond` and the re-dispatch of the failed Run start
every Ripple in the engine, while the Duck, whose ledger has the roots complete for that `f`, re-runs and reports
only `join_lines`. Check whether the throttle or memory slots read the stuck Ripples as busy. The reproduction
script is in the session scratchpad (`repro.sh`): demo, break, refresh, fix, then read the Ripple states.

## 18. An overtaken Run is recorded as failed (attention FINDINGS 15)

**Done 2026-11-15.** `_close_abandoned_runs` closes older running rows as `superseded`, with `superseded_by` (the
covering Run's freshness) and no error (migration `036`, a `pond_run` rebuild). Shown in `/api/runs`, `duckstring
runs` and the web UI's run history; no longer counted by `/metrics` or taken as a failed Pond's error. Earlier
`failed` ("abandoned") rows are left as they were, by decision.

## 19. One conflict rule for append tables (attention FINDINGS 12)

**Built 2026-11-18** (Trickle 0.4.0; `trickle.io.append_zset`'s `full`, `ON_CONFLICT`, `reject_removed`; tests in
`packages/trickle/tests/test_trickle_on_conflict.py`). `fail_on_conflict=False` meant two things: on `append_table` it skips the key
check and appends every row (duplicates included); on the builder's `.append()` it drops rows whose key is already in
the history (logged to `__droplog`). The showcase read it the builder's way and de-duplicated by hand in `wa_topics`.
The builder also has no unchecked write at all, though an append is chosen for speed: its conflict check and the
subtraction a recompute needs are fused in `append_zset`.

**The rule.** `fail_on_conflict` is removed from both. In its place `on_conflict`, the same values and default on
`append_table` and the builder's `.append()` (and `.accumulate().append()`):

- `"skip_check"` (default): no read of the history. Every row is appended. A declared `pk` is the writer's promise.
- `"keep_prior"`: rows whose key is already in the history are dropped; with no `pk` the whole row is the key.
- `"fail"`: a key already in the history with different values raises before writing.

Under `keep_prior` and `fail` a row identical to one already in the history is skipped (nothing to decide), and a
dropped row with different values is logged to `__droplog` when `log_drops` (default `True`, now on `append_table`
too). Within one batch: `skip_check` appends everything; the other two collapse identical rows and raise on one key
with different values (a batch has no order, so there is no "prior", and an arbitrary pick would differ on replay).
A write repeated at the same `f` replaces that `f`'s rows under every rule (`append_zset` doesn't delete at `f` today;
it must, and a recompute's subtraction excludes rows stamped `f`).

**The builder under `skip_check`.** Two steps, kept apart: produce this Run's rows, then write them with the rule.
- Incremental: the change itself, with no history read. A removal in the change is a broken promise, visible without
  reading history: with a `pk`, drop the key's added row too (the outcome `keep_prior` gives on a recompute); with
  no `pk`, ignore the removal and append the added row. A Source row deleted with nothing added is ignored.
- Recompute (coverage miss, a changed overwrite Source, over `p`, `.sql()`, `ivm=False`): the whole output minus
  the history, by `pk` (as `keep_prior`), copy for copy without one (`EXCEPT ALL`, so identical rows that are
  separate events survive). Never an overwrite: readers have read every row as added, and a recompute often follows a
  Source's retention trim, so it can't rebuild what the history holds.
- What's left: a key already in the history arriving again with no removal beside it is appended by an incremental
  Run and dropped by a recompute. That's what skipping the check costs; the docs say so.

The spine-key shortcut (`_spine_pk_passthrough`, item 5) becomes an optimisation of `keep_prior` with
`log_drops=False`; `skip_check` doesn't need it.

**Removal.** Passing `fail_on_conflict` raises `TypeError` naming the replacement, which differs by API (on
`append_table`: `False` → `skip_check`, `True` → `fail`; on the builder: `False` → `keep_prior`, `True` → `fail`).
Trickle's API changes, so the package goes to 0.4.0 and Duckstring's range moves with it. Update the demos, benches,
docs (`append_and_merge`, `joins_with_the_builder`, the `trickle_io` and `trickle_builder` reference), CHANGELOG and
CLAUDE.md. The attention showcase's ~20 calls change when it moves to the release (not while it runs on 0.8.0).

**Tests.** Each rule on both APIs: conflicts against the history, within a batch, identical re-sends, no `pk`
(repeated identical events kept under `skip_check`), replay at the same `f`. The builder under `skip_check`:
incremental reads no history (query counter); a dimension change on the incremental path and on a recompute leaves
the same table; a recompute after a Source retention trim keeps the history's rows. The removed argument raises
naming the replacement.

**Follow-up, measure first.** A Source whose change only adds rows still trips `p` and forces a recompute, though
joining the new rows is never more work. `p` may need to count only removals and changes.
