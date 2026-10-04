# Versioned overwrite tables on the Parquet plane

Status: **built** (2026-10-03). See "As built" at the end for where it differs from the design below.

## Problem

A Sink run's reads of a Source are meant to be pinned to the run's freshness: `Pond.read_table` passes
`as_of=self.f` for every foreign read (`core.py`), so every Ripple of a run sees the same version of each
Source, and every table of a Source comes from the same Source run. The Iceberg plane honours that from its
snapshots. The Parquet plane can't: an overwrite (plain Ripple) table is one `{table}.parquet`, replaced in
place by each Source run, so there is no older version to return and the read gets whatever is there.

So on the Parquet plane, a Source that republishes during a Sink's run can give that run some tables (or
some Ripples' reads) from the old Source run and some from the new one. The Parquet plane is what an
object-store data root uses, so this is the cloud setup's behaviour. Merge and append Trickle tables are
unaffected: their reads filter by the rows' `_duckstring_f`.

## Rejected: copying at run start

Copying each Source table into the Sink at run start (a "snapshot Ripple", or an opt-in flag that does it)
puts a full copy of every large overwrite table on the Sink's critical path, every run. It also has to be
named per run, since concurrent runs of a Pond share its registry and a `TEMP` table is private to one
Ripple's connection. A per-Ripple "depends on exact Source versions" flag would exist only to opt into
that cost.

## Design: don't destroy the previous version

Pinning needs no copy, only that the Source stops overwriting the version a Sink is reading. That costs
disk space while the old version is in use, and no time. It is therefore the default for every read, with
no flag.

### Write

Each run writes an overwrite table to a new key, `{table}__v/{part_name(f)}.parquet`, instead of over
`{table}.parquet`. One `COPY` of the same bytes as today, so the write cost is unchanged; on an object store
it is a new object rather than a replaced one. Local writes still go through a temporary name and a rename,
so a reader never sees a half-written version. `part_name`/`part_f` (trickle/io.py) already give a
canonical, sortable, reversible name for a freshness.

The `_trickle.json` sidecar keeps its per-table `f` (the latest version), so `read_delta`'s "did this
overwrite Source advance" check is unchanged.

### Read

`ParquetDataPlane._raw_read_select(data_dir, table, as_of=...)` lists `{table}__v/` and reads the newest
version with `f <= as_of`, or the newest version when `as_of` is `None` (the data viewer, `/api/query`,
serving). Version selection is by file name: overwrite tables carry no `_duckstring_f` column (it's
reserved), so the row-level predicate `_read_parquet_glob` uses for parts doesn't apply.

Concurrent Sink runs each read at their own freshness, so each gets its own version with no bookkeeping.

**Fallback.** If no version satisfies `as_of` (the pinned one was pruned, for example after a crash lost
the retention information below), read the newest version and log a warning. That is today's behaviour; a
run never fails for it.

### Retention

A superseded version is kept while a Sink run in flight may read it, and the Catchment knows exactly which
those are.

- `Driver` adds `retain_from` to each `begin_run` job: the oldest freshness among this Pond's Sinks' runs
  in flight (their `start_f`, since a Sink reads at `as_of = its f`), or `None` when no Sink run is in
  flight.
- At export, after writing the new version, the Duck deletes every version older than the version
  `retain_from` resolves to (the newest with `f <= retain_from`). It never deletes a version published
  after its own run started: Source runs can overlap (a Wave pipelines them), and a Sink that started after
  an overlapping run published may be reading that version without this run's job knowing.
- So a large table exists twice only while some Sink run is actually reading the old copy. Deletion is
  cheap and can run after the new version is published, off the critical path.

Why this is enough: a Sink run in flight when the job was built is covered by `retain_from`; one that
starts later reads a version at least as new as the newest at this run's start, which is never deleted.

### Other readers and writers

- **Persist** (`dataplane.persist_tree`): directory files are already immutable and idempotent by name,
  and local deletions propagate to the mirror, so `__v/` mirrors with no change. It now uploads each version
  once instead of re-uploading `{table}.parquet` on every persist.
- **Listing and serving** (`list_tables`, `table_path`, `files_for`): resolve to the newest version.
- **Draws** (`routes/draw.py`, `poller._land_transfer`): ship the newest version only; the consumer lands
  it as its own `__v/` version under the upstream `f`.
- **Spouts** could read the Source at the job's `source_f` the same way. Optional; delivering the newest
  version is fine.
- **Iceberg plane**: it writes its flat Parquet layer through `ParquetDataPlane.export`, so it would write
  `__v/` too. See `plans/data-plane-choice.md`, which may remove the Iceberg plane altogether.
- **Earlier layouts**: there are no external users, so `{table}.parquet` needn't be read as a fallback.
  Keep reading it only if that turns out to be trivial.

## Tests

- A Sink run pinned at F1 reads F1's version while the Source publishes F2 mid-run, for every table of
  the Source and across two Ripples of the Sink.
- Two concurrent Sink runs at different freshnesses each read their own version.
- Overlapping Source runs: a version published by an overlapping run isn't pruned while a Sink reads it.
- Pruning keeps exactly what `retain_from` and the run-start rule require; without Sink runs in flight,
  only the newest version remains.
- The fallback: a pruned pin reads the newest version and warns.
- The Persist mirror, a Draw and the data viewer see the newest version.
- An object-store data root (moto, and MinIO in CI).

## Effort

About a day to a day and a half.

## As built (2026-10-03)

Four things changed from the design above, agreed with the author before building.

**The pin is the Source's published freshness, not the Sink's.** The design pinned every read to
`as_of = Sink f`. But a Sink's `f` is the minimum over its required Sources' `end_f`, so when Sources have
diverged (one also feeds another Outlet's Wave, or was tapped alone), reading the fresher Source "as of" the
Sink's `f` selects an older version than the run was scheduled on, which can be staler than the `f` the
Sink stamps, and with retention that older version is usually gone (a fallback warning every run). Each
overwrite Source is instead read at the newest version at or below its **pin**: the Source's `changed_f`
when the Sink run was dispatched, which the `begin_run` job already carried as `source_f`. Trickle reads
stay bounded by the Sink's `f`, which their delta windows need. Without a job (puddle runs) the run's own
`f` is used.

**Pins are persisted per run.** The Duck held one `executor.source_f`, overwritten by every `begin_run`, so
pipelined runs would have shared pins. The Duck now keeps inputs per Run freshness (`RunInputs`, first job
wins, a Force replaces them), and the Catchment records them in `pond_run.source_pins` (migration `029`).
A re-dispatch after a Catchment restart re-sends the original pins, and retention reads them back.

**The retention rule closes a gap.** As written, a Source run could delete the version that was newest when
it started: a Sink dispatched between the Source run's job and its publish is pinned to that version, and
neither `retain_from` (built earlier) nor the "published after the run started" rule covers it. So
`retain_from` is the minimum of the in-flight Sink runs' pins on this line **and the line's own
`changed_f` at dispatch**. That subsumes the "published after the run started" rule (such a version is
newer than `changed_f`), so the Duck applies one rule: keep the version `retain_from` resolves to and
everything newer. A Sink run with no pin for this line reads at its own `f`, so that bounds it instead;
`running` rows at or below the Sink's `end_f` are stranded and ignored.

The cost is that a publish leaves the previous version behind (it was the newest at dispatch). To get back
to one copy at rest, the reap's `shutdown` job also carries `retain_from` and the idle Duck prunes before it
exits, then persists. A Draw has no Duck, so `complete_draw_transfer` prunes the landed versions in the
Catchment, where the bound is exact.

**Persist needed an explicit watermark.** `_mirror_dir` never prunes a destination file for being absent
locally (plans/s3-resident-state.md), so local deletions did not propagate as the design assumed. Each
overwrite table's sidecar entry now carries `v_floor` (the oldest retained version, carried across the
per-publish sidecar rewrite by `publish_plan`), and the mirror prunes `__v/` files below it.

Smaller points:
- An export with `f=None` versions under the current time; Trickle companions exported without `f` keep the
  single-file layout they had.
- A single `{table}.parquet` is still read when a table has no versions: Puddle snapshots use that layout.
- The ripple download route (`files_for`) serves the newest version under the name `{table}.parquet`.
- Spouts are pinned like Sinks (their run row records pins; the worker reads `read_select(..., pin=)`), and
  the `mode=append` mirror holds only the newest version.
- The warm serving connection for full-access users holds views onto specific version files. A query in the
  short gap between a prune and the next cache rebuild (on the publish's `data_version` bump) can hit a
  deleted file. Accepted rather than adding machinery.
