# Per-object change detection

Status: **planned** (2026-10-11). Builds on `plans/no-change-skip.md` (the Pond-level `changed_f`, the engine
pass, `pond.skip()`), `plans/versioned-overwrite.md` (versions and pins) and `plans/objects.md`.

## Problem

The no-change skip works at Pond granularity. `pond.skip()` holds the Pond's `changed_f`, and a downstream
Pond whose Sources all held theirs is passed by the engine with no Duck. That leaves three gaps:

1. **No way to tell which outputs changed.** A Pond that publishes several tables, where only some
   changed, must either skip as a whole (wrong) or not skip at all, and then every consumer recomputes
   everything. A consumer can't ask "did `products.product` change?" and skip only the work that reads it.
2. **Plain overwrite tables carry no change signal at all.** Every run writes a new version and stamps the
   sidecar `f`, so to a consumer an overwrite table always looks changed, even when its content is
   identical. This also forces a downstream Trickle into its comprehensive path whenever an overwrite
   dimension Inlet runs (`read_delta` returns `is_full` because the sidecar `f` advanced), and it writes a
   redundant copy of every unchanged table on every run.
3. **Inlets have no supported pattern.** An Inlet is where change is first known, and the only tool is a
   hand-rolled comparison followed by `pond.skip()`.

## Decisions (settled with the author)

- **Change is tracked per published object** (table or Object), not per Ripple. Ripple-level skips were
  considered and dropped: per-object change plus a terminal-Ripple `pond.skip()` covers the same ground.
- **`pond.skip()` stays explicit and stays the only thing that holds the Pond's `changed_f`.** Nothing
  in this plan skips a Pond automatically. Many Ponds exist for a side effect (an API call), and an
  automatic skip there would be a dangerous surprise. The engine stays Pond-level; per-object change is
  consulted only inside a Duck, by Ripple code.
- **Trickle tables and Objects get change freshness automatically**, because it falls out of what is
  already published (per-run parts; an Object is only published when written) at no extra cost.
- **Plain overwrite tables are opt-in**, because fingerprinting costs a scan. Without opting in, an
  overwrite table behaves exactly as today: a new version every run, counted as changed.
- **The API hangs off the Pond handle, not the relation.** `rel.detect_change("orders")` was considered.
  DuckDB's `DuckDBPyRelation` accepts a class-level method (checked on 1.5.6) but an instance carries no
  state and exposes no public link to its connection, so the method couldn't find its Pond except through
  a thread-local that breaks as soon as a Ripple uses threads. It would also patch a class we don't own for
  every DuckDB user in the process.
- **No file-hash comparison for tables.** Parquet bytes aren't deterministic for identical content
  (parallel `COPY` row order, row-group boundaries, writer metadata), so it would mostly report "changed",
  and making it deterministic (sort plus single-threaded write) costs more than the row-hash aggregate,
  after a full write. File hashing remains the right tool for an Inlet that downloads files, in the
  user's own code.

## Concepts

**Change freshness** of a published object: the freshness `f` of the run that last changed its content.
It is always `<=` the Pond's `end_f`. The Pond's own `changed_f` is unchanged by this plan; it is still
set from `pond.skip()` alone.

**The change ledger**: the Duck's per-Pond-Run record of what each Ripple wrote, checked or held, keyed
by run `f` (concurrent Runs share a registry, as `_skipped` already handles) and by Ripple attempt (an
immediate retry clears that Ripple's entries before it re-runs). Export reads it to decide which tables
are held. It is in memory: after a Duck crash it is lost, the affected tables count as changed and are
republished. That is a redundant write, never a missed update.

## API (Pond handle)

```python
pond.detect_change(name, value, *, exact=False) -> bool
pond.write_table(name, relation, *, if_changed=False, cluster_by=None, ...) -> bool
pond.write_object(name, src, *, if_changed=False) -> bool
pond.changed(*refs) -> bool
pond.skip() -> None             # unchanged
pond.sources_changed() -> bool  # unchanged
```

### `detect_change(name, value, *, exact=False)`

Whether `value` differs from what is currently published as the own object `name`. `value` is a DuckDB
relation (a table) or bytes / a path / a binary file-like (an Object), dispatched on type. It never writes.
It returns `True` (changed) whenever it can't prove otherwise: no recorded fingerprint, a fingerprint from
a different format version, or a registry that doesn't hold the published content (below).

Calling it records `checked(name, fingerprint)` in the ledger. If the result is `False` and the Ripple does
not then write `name`, the object is **held** for this run.

`exact=True` (tables only) compares with `EXCEPT ALL` in both directions against the registry table
instead of comparing fingerprints. Slower, but can't collide. Only valid when the registry holds the
published content (same precondition as below); otherwise it returns `True`.

The return value is advice: writing anyway is always allowed and always counts as a change. That is how a
user handles collisions their own way (for example a fingerprint match confirmed by a row count or an
`exact` check before deciding).

### `write_table(..., if_changed=True)` and `write_object(..., if_changed=True)`

Shorthand for `if pond.detect_change(name, value): write(...)`. Return whether they wrote. Plain
`write_table` returns `True` (it always writes), replacing today's `None` return. `write_object` today
returns `None`; it returns `True` likewise.

### `changed(*refs)`

`True` if any of `refs` changed, as seen by this run:

| Reference | Changed when |
|---|---|
| `"source.table"`, overwrite | the version this run resolves at its pin has `part_f > previous_f` |
| `"source.table"`, append or merge Trickle | some part in `(previous_f, f]` has rows (history / `__changelog` / warm band), or the window isn't covered (bootstrap, coverage miss) |
| `"source.object"` | the Object's sidecar `f > previous_f` |
| own table or Object | written this run (and not held); for a Trickle table, its write reported a change |
| no arguments | any own object written-and-changed this run (the terminal-Ripple pattern) |

Own references go through `_check_own_read`, so asking about a table before its writer has run raises
`RippleOrderError`, as for `read_table`. The no-argument form only counts objects the handle saw this run
(written through the handle or checked with `detect_change`); tables written with raw SQL on `pond.con`
are invisible to it, which the docs state.

On `previous_f` as the operand: in a diamond, a non-binding fresher Source may already have been read at a
version newer than `previous_f`, so `changed` can report `True` for a change the previous run already saw.
That is a redundant recompute, never a missed one, and matches the Pond-level rule
(`_pond_sources_changed`). A precise operand (the previous run's pin per Source, already in
`pond_run.source_pins`, sent on the `begin_run` job) is a possible later refinement; not in scope.

Local runs (`pond run`): `previous_f` is `NEVER`, so every Source reference reports `True`. Own references
work as in a deployed run, which makes the terminal-Ripple pattern testable locally. `pond run` resets
`puddles/out/`, so `detect_change` has no recorded fingerprint and returns `True`.

The terminal-Ripple pattern this enables:

```python
@ripple(parents=[load_rates, load_products])
def done(pond):
    if not pond.changed():
        pond.skip()
```

And selective downstream work:

```python
@ripple
def enrich(pond):
    if not pond.changed("products.product"):
        return  # this Ripple's output stays as it was
    ...
```

## Mechanism

### Overwrite fingerprints

`fingerprint(con, relation) -> str`, a new helper in `dataplane.py` (not `trickle/`, which stays
host-independent):

- `SELECT count(*), sum(hash(t)::HUGEINT) FROM (relation) t` (verify `hash` over a whole-row struct on the
  floor DuckDB; fall back to `hash(COLUMNS(*))`). Order-independent, and duplicate rows are counted (a
  plain XOR would cancel them).
- Combined in Python (sha256) with the schema (column names and types, in order) and a format tag that
  includes the DuckDB major.minor, because `hash()` isn't guaranteed stable across DuckDB versions. A tag
  mismatch makes `detect_change` return `True`, so an upgrade costs one redundant republish per table,
  never a false "unchanged".
- Row order and `cluster_by` reordering don't change it. A type change does.
- Floating-point quirks (`-0.0` vs `0.0`, NaN payloads) follow DuckDB's `hash`; documented, not handled.

### Where fingerprints live, and why the registry needs one too

The baseline is the **published** fingerprint, held in the table's sidecar entry (`"fp"`). The registry
can't be the baseline on its own: a failed run leaves its writes in the registry without publishing them,
so a registry-only fingerprint would let the next run compare against content no consumer ever saw.

But the registry still matters, because own downstream Ripples read the registry, and a held table must
leave the registry holding exactly the published content. Case: run 1 writes `T = new` and fails (not
published); run 2 computes `T = old`, matches the published fingerprint and holds, but the registry still
holds `new`, which later Ripples in run 2 read. So `detect_change` returns `False` only when **both** hold:

1. `fingerprint(value) == sidecar[name].fp`
2. the registry's recorded fingerprint for `name` equals `sidecar[name].fp`

The registry fingerprint lives in a new registry-only table `_duckstring_fp(table, fp, f)` (the
`_duckstring_` prefix keeps it unpublished; `RESERVED_PREFIX` already covers it). It is written by
`write_table` when it has a fingerprint (from `if_changed=True`, or a preceding `detect_change` on the same
relation object in the same Ripple), and **deleted** by any other `write_table` of that table, so a write
without a fingerprint can never leave a stale one behind. Raw SQL on `pond.con` that changes a table after
a fingerprinted write is not seen; this is the one documented gap of an opt-in feature.

Registry hydration (`hydrate_registry`) does not restore `_duckstring_fp`, so after a registry loss the
first `detect_change` per table returns `True` and the write restores it. Refresh (`executor.wipe`) drops
it with the registry.

The sidecar copy is read from the Pond's own `data_dir` (one small file; on an object store, one GET per
run, cached on the handle for the run).

### Export of a held table (`ParquetDataPlane.export`, `publish_plan`)

`export` takes the ledger's held set (a new `held=` argument, threaded from `DuckCore` through
`executor._export_data`). For a held overwrite table:

- no new version is written under `{table}__v/`;
- `publish_plan` carries the prior sidecar entry forward unchanged (`f` and `fp` stay at the run that last
  changed it), instead of stamping this run's `f`;
- retention is unaffected: the newest version is the held one, and `retained_versions` always keeps the
  version a pin resolves to.

For a non-held overwrite table written with a fingerprint, the sidecar entry gains `"fp"`. For one written
without, `"fp"` is dropped.

Because the sidecar `f` of an unchanged table now stays put, `read_delta` on a held overwrite Source
returns an **empty** delta, so a downstream builder treats it as a stable operand instead of recomputing
in full. This is the largest win for Trickle consumers of plain dimension Inlets. While here, fix
`read_delta`'s overwrite branch to resolve the version at the pin (`select_version`) rather than
`min(src_f, pin)`, the same resolution `changed` uses.

### The sidecar's run stamp

Today the plane's freshness (`registry._plane_freshness`, the max `f` over sidecar entries and Objects) is
compared with the Catchment's `changed_f` by `resolve_data_dir` to detect a stale local publish. Once
entries can hold an older `f`, a run that changes nothing it publishes but doesn't call `pond.skip()`
(Pond `changed_f` = run `f`) would leave every entry older than `changed_f`, and `resolve_data_dir` would
wrongly treat the local publish as stale.

Fix: the sidecar gains a top-level `"run": {"f": ...}` written on every export, and `_plane_freshness`
reads it first (falling back to the max over entries for sidecars written before this change). Every
sidecar consumer that iterates entries as tables must skip `"run"` as it skips `"objects"` and `"state"`:
audit `prune_versions`, `list_tables`, `hydrate_registry`, `publish_plan`, `_enrich_sidecar`,
`routes/draw.py`, `routes/data.py`, `poller._land_transfer`, `egress/object_store.mirror`, the Persist
mirror. A shared `sidecar_tables(sidecar)` helper in `trickle/io.py` replaces the ad-hoc skips.

`routes/data.py` trace (`row_f` for a plain table) now reports the run that produced the content rather
than the latest run, which is more correct; no change needed beyond the audit.

### Objects

- The digest is a sha256 over the payload: bytes in memory; a file streamed in place; a directory as a
  sorted list of `(relative path, size, sha256)`; a file-like read once (as `stage_object` already does).
  No staged copy is needed to compare. The baseline is the Object's sidecar entry, which gains `"sha256"`.
- `write_object(..., if_changed=True)` computes the digest while staging (the copy happens anyway) and,
  when it matches, unstages instead of staging. It also removes any stale staged payload for that name left
  by a failed earlier run, so a held Object can't be committed later by accident.
- `commit_objects` writes `"sha256"` into each committed entry. An Object written without `if_changed`
  still records its digest (it is computed during the copy at little cost), so a later `if_changed` check
  has a baseline.
- An Object that isn't written keeps its sidecar entry and `f`, as today.
- The Object registry-consistency problem above doesn't arise: an own `read_object` reads the staged
  payload, else the published one, never a registry copy.

### Trickle tables

- Own: `append_table`, `merge_table`, `apply_zset` and builder terminals already return or expose whether
  they changed; they record `written(name, changed)` in the ledger. Nothing changes at export (their
  parts already encode change).
- Source: `changed("src.t")` lists parts in `(previous_f, f]` (`table_parts`, `part_f`) and checks row
  counts from Parquet footers (`parquet_metadata`), without reading data. A 0-row marker part counts as no
  change. Coverage is checked against the sidecar `floor` exactly as `_covered` does.

### Duck plumbing

- `DuckCore` gains the ledger (replacing nothing; `_skipped` stays for `pond.skip()`), with
  `record(f, ripple, attempt, name, kind)` and `held_for(f)`. `__main__` passes a `change_sink` alongside
  `skip_sink`, through `executor.make_pond` and `RippleExecutor.submit`, into the `Pond` handle.
- `executor._export_data(..., held=core.held_for(f))`.
- `DbtExecutor`: no change detection in v1 (models are plain overwrite nodes run by dbt).
- `local/runner.py`: a ledger per run, so `pond.changed()` on own objects works locally; held tables are
  honoured by the local export.

## Deliverables (in order)

1. **Ledger and own-object change.** The `DuckCore` ledger and `change_sink`; Trickle writes recording
   into it; `pond.changed()` for own objects and the no-argument form; `write_table`/`write_object`
   returning `bool`. Tests: `tests/test_change_detection.py` (new) for the ledger (retry clears a Ripple's
   entries; concurrent run `f`s don't mix), `RippleOrderError` on an early own reference.
2. **Overwrite fingerprints and held tables.** `fingerprint`, `_duckstring_fp`, `detect_change` (incl.
   `exact`), `write_table(if_changed=)`, held export (no version, sidecar carried), the sidecar `"run"` stamp
   and `sidecar_tables` audit, the `read_delta` pin fix. Tests: identical relation → held, no new version
   file, sidecar `f` unchanged; changed relation → new version; failed-run-then-revert case (registry ≠
   published → `True`); fingerprint ignores row order, sees duplicates and type changes; format-tag
   mismatch → `True`; `resolve_data_dir` not fooled by held entries (`tests/test_platform.py` style);
   `read_delta` empty for a held Source (builder stays incremental, assert via `.was_changed()` /
   comprehensive-path counters in `tests/test_trickle.py`); retention with a held newest version
   (`tests/test_versioned_overwrite.py`); object-store variant in `tests/test_object_store.py`.
3. **Objects.** Digest, `write_object(if_changed=)`, stale-stage cleanup, `sha256` in the sidecar. Tests
   in `tests/test_objects.py` (bytes, file, directory; a held Object keeps its `f`; draw doesn't re-ship
   it, `routes/draw.py` already gates on `f`).
4. **Source-side `changed`.** Overwrite at the pin, Trickle parts in the window with footer counts,
   Objects. Tests: unit, plus a `tests/test_runtime.py` e2e: an Inlet with two tables where one is held,
   a Sink Ripple that returns early on the unchanged one and recomputes on the other, and a terminal
   `if not pond.changed(): pond.skip()` that makes the next Pond engine-pass (no Duck spawned).
5. **Docs** (below), run against the real builder for every example that changes.
6. **CLAUDE.md**: a short "Change detection" subsection under Orchestration model (ledger, opt-in
   fingerprints, held export, the `"run"` stamp), and the sidecar note under Data plane.

Run `ruff check .` and the engine, trickle, runtime and object-store suites before each push.

## Documentation changes

Follow `docs/documentation_style.md`. Use the demo pipelines: `transactions`, `products` → `sales` →
`reports` for plain Ponds, `orders`, `catalog` → `priced` → `revenue` for incremental ones.

### New guide: Ingesting Data (`docs/docs/guides/ingesting_data.md`)

A new sidebar category **Getting Data In**, placed after Building Ponds and mirroring Getting Data Out. It
is one of the first pages a new user looks for, so the opening should get to a working Inlet quickly.
Outline:

1. **Inlets.** What an Inlet is (a Pond with no Sources), why it is the only place that knows whether the
   outside world changed, and the freshness it takes (the demand epoch, or a window end).
2. **Reading from outside.** DuckDB does most of the work: files and object storage (`read_parquet`,
   `read_csv`, `read_json` over `s3://`/`https://`, httpfs), databases (the `postgres`/`mysql` extensions,
   `ATTACH`), HTTP APIs (fetch in Python, then a relation; no replacement scans, per the conventions).
   Credentials from the environment of the Duck (and how that reaches cloud Ducks).
3. **Choosing a table type.** Snapshot (`write_table`), event stream (`append_table` with a `pond.f`
   stamp or a source watermark), current state with CDC (`merge_table`). Links to Append and Merge.
4. **Incremental pulls.** Reading your own previous output for a high-water mark (`max(updated_at)`),
   replay safety via `pond.f`.
5. **Skipping when nothing changed.** The cheapest check first: an ETag or last-modified header, a file
   listing, a source-side `max(updated_at)`; store the last seen value in a `_duckstring_` table and call
   `pond.skip()`. Then `write_table(..., if_changed=True)` / `detect_change` for snapshot tables, what it
   costs (one scan) and when it pays (a downstream Trickle stays incremental; Sinks can skip). Merge and
   append tables need nothing. The terminal-Ripple `if not pond.changed(): pond.skip()` for an Inlet with
   several tables. Collisions and `exact=True`, briefly.
6. **Files and models as Objects.** `write_object(..., if_changed=True)` for a downloaded artefact.
7. **When it runs.** Windows on Inlets for batch sources, triggers on Outlets (link Scheduling and the
   Playground, plain URL).
8. **Testing an Inlet locally.** `pond run`, what `changed`/`skip` do locally.

`writing_ripples.md`: shorten "Inlets: reading from outside" to two sentences and a link to the new guide.
`sidebars.ts`: add the category and page. `docs/docs/concepts/ponds.md`: link "Inlets ingest from external
systems" to the guide.

### Concepts

- `concepts/orchestration.md`, "Skipping unchanged work": expand from one sentence to the full picture,
  without code: a run that changes nothing it publishes can say so; Inlets decide this because only they
  can see outside; downstream Ponds then pass without running; each table and Object also carries its own
  change freshness, so a Ripple can skip just the work that depends on an unchanged input.
- `concepts/trickles.md`: one sentence noting that a plain Source checked for change no longer forces a
  full recompute when it hasn't changed.

### Guides

- `writing_ripples.md`, "Ripples with side effects": keep `always_run`/`sources_changed`; add the
  terminal-Ripple `pond.changed()` pattern and the note that `skip()` from any Ripple marks the whole run.
- `append_and_merge.md`, "Skipping downstream work": use `pond.changed()` in the multi-table case.
- `joins_with_the_builder.md`: where it explains that a changed plain table forces a full recompute, add
  that an unchanged one checked with `if_changed=True` doesn't.
- `testing_with_puddles.md`: what `changed`, `detect_change` and `skip` do in `pond run`.

### Reference

- `reference/python/pond.md`: `write_table` (`if_changed`, `bool` return), `write_object` (`if_changed`,
  `bool` return), and the "Skipping unchanged work" section rewritten as "Change detection": `changed`,
  `detect_change`, `skip`, `sources_changed`, with the reference table above (in prose-friendly form) and
  the raw-SQL caveat.
- `reference/python/trickle_io.md`: `read_delta` on a plain Source is empty when the Source held the table.
- `reference/python/trickle_builder.md` (the `p` / plain-table paragraph): the same.
- `reference/python/decorators.md`: the `always_run` example gains the `changed()` variant.
- `reference/orchestration_theory.md`, the `changedF` section: per-object change freshness exists and is
  consulted by Ripple code only; the engine's pass rule is unchanged.
- Docstrings in `core.py` (`write_table`, `write_object`, `detect_change`, `changed`, `skip`) written to
  match `reference/python/pond.md`, as for the other public docstrings.

## Out of scope

- An engine-level pass per table (a Pond reading only an unchanged table of a changed Source passing
  without a Duck). It needs per-table state in the Catchment and the declared reads of every Ripple.
- The precise per-Source previous-pin operand for `changed` (see above).
- Change detection for dbt-mode Ponds.
- Skipping the republish of untouched overwrite tables that were never checked. They are republished as
  today, because a raw-SQL write to them can't be seen.
