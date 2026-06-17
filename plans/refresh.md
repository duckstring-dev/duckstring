# Refresh & Repair: full rebuild and propagated repair

Status: **designed, unbuilt.** Three interlocking pieces, building on Trickle (`plans/trickle.md`,
`src/duckstring/trickle_io.py`): (1) a **bugfix** so a consumer absorbs a discontinuous read correctly,
(2) a per-Pond **refresh** flag (full rebuild on next run), and (3) **repair** — force-refresh a chosen
set now. Ship in that order; each is usable on its own and the later ones lean on the earlier.

The orchestration principle settled in design: a refresh is **"a run in refresh mode,"** never a
recompute *at* a freshness. It changes *how* the next run computes (full rebuild + raise the changelog
floor), not *when* or at what freshness — so it propagates honestly through the existing freshness model
instead of lying about currency. Repair is the imperative escape hatch for "fix it now," for when no new
freshness is coming to carry the refresh downstream.

---

## Deliverable 1 — Bugfix: comprehensive absorption of a discontinuous read

**The bug (live today, triggerable by retention).** `read_delta` already falls back to a full read when
the consumer's window can't be covered (`previous_f < the changelog's oldest stamp`). But the full-read
branch returns `deletes = ∅` — it *can't* compute deletes, because they depend on the consumer's prior
state, which `read_delta` doesn't have. A **comprehensive** consumer (`read_table` + `merge_table(
comprehensive=True)`) is safe (it diffs against its own state). A **partial / builder** consumer that
trusts `delta.deletes` silently **keeps stale rows** after any discontinuity — and `retain_n`/`retain_t`
trimming the changelog past a lagging consumer triggers exactly this, with no wipe needed.

**Fix.**
- `Delta` gains an **`is_full`** flag — `True` on the bootstrap / coverage-miss / overwrite-source
  branches of `read_delta`, `False` on the windowed branch.
- The **builder** (`trickle_builder.TrickleBuilder.merge`) branches on it: if **any** source delta is
  full, recompute the *whole* output (`_recompute(affected=None)` — the full join, no key restriction)
  and `merge_table(comprehensive=True)`; otherwise the partial `affected`/`dropped` path as today. "Any
  full → comprehensive" is deliberately conservative and always correct (a comprehensive merge against
  the builder's own main computes the right deletes), and it makes the builder robust by construction.
- **Hand-rolled** partial consumers get one documented rule (python-api.md + the Trickle guide): *on
  `delta.is_full`, do a comprehensive merge.* This is also why the lazy/repair propagation below
  terminates cleanly — a consumer that absorbs a discontinuity comprehensively re-emits an ordinary
  delta downstream.

**Tests.** `read_delta` sets `is_full` correctly per branch; a retention-lag repro (small `retain_n`,
producer drops a row past a builder consumer's watermark) asserts the dropped row leaves the consumer's
output *with* the fix and lingers *without* it; the builder bootstrap path still passes.

This is standalone — no engine or UI changes — and lands first.

---

## The coverage floor (shared mechanism for 2 & 3)

Refresh needs to *raise the changelog floor* so downstream coverage-misses and reloads. Today coverage
keys off `min(changelog rows)`. Generalise that to an explicit **`floor`** carried in the published
`_trickle.json` sidecar (and the Iceberg `duckstring.floor` table property, which already exists):

- `read_delta` coverage becomes `previous_f < floor → full read` (floor defaults to `min(changelog rows)`
  when unset, so existing behaviour is unchanged).
- **Retention** advances `floor` as it trims (it already computes this).
- **Refresh** sets `floor = the refresh run's freshness`, so even an emptied/rebuilt changelog signals
  "everything before me is not covered — full-read the main."

One concept now serves retention, the coverage check, and refresh.

### A first run *is* a refresh (and today it's wasteful)

You're right that a first run is conceptually identical to a refresh, and the current code is wasteful: a
merge Trickle's first run (`main_exists = False`) dumps **every** row into the changelog as an upsert. But
those rows are **never consumed** — a first-time consumer reads with `previous_f = NEVER` and bootstraps
from the *main*, and no consumer can have a window whose lower bound predates the first run. So the entire
first-run changelog is dead weight (and a refresh, which re-emits from empty, has the same waste).

The floor fixes both: **on any bootstrap (`main_exists = False`, i.e. a genuine first run *or* a refresh),
write the main and an *empty* changelog with `floor = run.f` — skip the all-rows upsert dump.** A
bootstrapping consumer full-reads the main (correct); a later consumer reads only real per-run deltas; a
consumer that somehow predates the source coverage-misses on the floor and full-reads. So first-run and
refresh collapse into one path, and the wasteful changelog disappears. (Append Trickles are unaffected —
their single table *is* the data, so there's nothing duplicated to skip.) This updates
`test_merge_comprehensive_diffs_insert_update_delete`, whose expected changelog currently includes the
run-1 upserts.

---

## Deliverable 2 — Refresh flag (lazy, honored on next run)

**Semantics.** Setting *refresh* on a Pond marks it so its **next run is a cold rebuild**: the Duck wipes
the Pond's registry tables (main + changelog + append + Pond-authored tables) before executing, the run
is dispatched with **`previous_f = NEVER`** (so its source reads are full and its own merges are
comprehensive-from-empty), and the data-plane export **overwrites** the changelog (not append-commit) and
stamps **`floor = run.f`**. The flag clears on a successful refresh run.

Wiping makes it uniform across Pond kinds (append/partial/hand-rolled can't self-correct via a
comprehensive diff, so they genuinely need the rebuild). Accepted cost: an append Trickle's per-run
historical granularity collapses to the rebuild's single `f` — a refresh only recovers what the sources
can still reproduce, by definition.

**Propagation is lazy and automatic.** The refresh takes effect on the Pond's *next* run, which happens
at a **genuinely new freshness** (a real upstream advance). That run's `floor = f_new` rises above
downstream consumers' watermarks → they coverage-miss → they absorb comprehensively (Deliverable 1) →
they re-emit ordinary deltas to *their* children. The signal travels one hop and is consumed — no manual
cascade, no freshness lie. Often you set the flag and simply let the next end-to-end run heal everything;
frequently you don't even need it (a comprehensive Trickle re-emits a correction delta on its own next
run).

**Changes.**
- **DB/engine**: a `refresh_pending` flag on `pond_state` (persisted, restored on reload). `engine`
  `refresh_pond(state, key)` sets it; consumed when a run for that key is dispatched.
- **Driver**: when draining `pending_begin_runs`, if `refresh_pending`, dispatch the `begin_run` with
  `previous_f = NEVER` + a `refresh=True` job field; clear the flag on the run-completed event.
- **Duck/executor**: on `begin_run(refresh=True)`, drop all registry tables before the first ripple;
  thread `refresh` into `export`.
- **Data plane**: `export(..., refresh=False)` — when `True`, append/changelog tables overwrite-commit
  (drop old published rows) and reset `LAST_F_PROP`/`FLOOR_PROP`; the sidecar `floor` is set to `f`.
- **read_delta**: coverage keys off the sidecar `floor` (above).

**Surface.**
- CLI (`cli/control.py`): `duckstring control refresh {pond} [-m MAJOR]` to set, `--clear` to unset. A
  one-shot that returns immediately (the flag is pending, not a run).
- API: `POST /api/ponds/{name}/refresh?major=` (set), `DELETE` or `?clear=true` to unset.
- Status: `/api/status` per-Pond carries `refresh_pending: bool`.
- UI: a small **Refresh** toggle on the Pond in the Sidebar (near Control), and a badge on the node when
  pending, so it's visible that the next run will rebuild.

---

## Deliverable 3 — Repair (force-refresh a set, now)

For "fix it now" with no incoming freshness to carry the lazy refresh. Repair **steps out of the demand
model on purpose**: it is a one-off, Driver-sequenced topological execution of refresh-runs over a chosen
**connected** set of Ponds (your relaxed rule below).

**Why imperative.** A refresh forced at the *current* freshness can't propagate via the floor (the
consumers' watermark equals the new floor → no coverage miss), and we won't fake a fresher stamp. So
repair doesn't rely on propagation at all: it explicitly runs *each in-scope Pond* in refresh mode
(`previous_f = NEVER`), in topological order, so every node full-reads its parents' freshly-rebuilt
output. No coverage subtlety, no freshness lie — just an ordered rebuild.

**The repair plan (new, bounded Driver orchestration).** Given a scope `S`:
1. **Quiesce by blocking.** Every Pond in `S` is marked **blocked** with a new `repairing` sub-reason the
   moment the repair starts, so normal demand can't start it **partway** with a stale/partial input set;
   `derive_blocked` propagates the block to downstream-of-`S` too. Standing triggers are suspended and any
   in-flight run in `S` is killed first (the existing pre-reset guidance). The repair plan — not demand —
   is the only thing that runs an `S` Pond while it's blocked.
2. Compute the **induced topological order**. For each `P ∈ S`, its *repair-parents* are the parents of
   `P` that are in `S`. Dispatch `P`'s refresh-force when all its repair-parents have completed; roots of
   `S` (no in-`S` parent) start immediately. Driven by run-completed events, exactly the "work through a
   task list" shape. A Pond's `repairing` block clears as **its** refresh completes, so the canvas
   animates progress and a node never runs before its turn.
3. On completion of all of `S`, release the quiesce and resume normal demand.
4. A node that **fails** stalls its descendants in the plan (they'd read un-repaired data); surface it,
   and allow abort / retry-from-here. Mirrors how a failed Pond blocks downstream.

**Scope validation — reject *disconnections*, not parallel branches.** `S` is valid when every pair of
selected Ponds that is connected in the full DAG stays connected **within `S`**: for each `X ∈ S`, every
`S`-member reachable from `X` in the full graph must also be reachable from `X` using only edges inside
`S`. Reject otherwise, naming the broken pair.

On the diamond `A→B, A→C, B→D, C→D`:
- `{A, B, D}` is **accepted** — `D` is reachable from `A` via `A→B→D`, a path entirely inside `S` (the
  parallel `C` branch is simply not in scope).
- `{A, D}` is **rejected** — `D` is reachable from `A` only through `B`/`C`, neither selected, so the path
  is broken; `D` would rebuild from entirely stale parents.

This is your relaxed rule (a connected path *through the selection* must exist), weaker than strict
convexity. The cost it permits: a selected Pond may read an **unselected parent that descends from a
selected root** (here, `D` reads the un-refreshed `C` while `A` was rebuilt) — so `D` is consistent along
the repaired path but may be inconsistent with that parallel branch until it's repaired or heals. The CLI
and UI **warn** (non-blocking) when a selected Pond has such an affected-but-unselected parent. A
*trailing* downstream Pond left out of `S` entirely is fine — it heals on its next genuine run.
`--downstream` adds the full downward closure of the seed set, which always satisfies the rule.

**Surface.**
- CLI (`cli/control.py`): `duckstring control repair {pond}... [--downstream] [-m MAJOR]` — accepts a set,
  `--downstream` includes all downstream. Rejects a disconnected set (a broken path through the selection) naming the broken pair; warns on affected-but-unselected parents. Opens
  the live status until the plan settles (like other one-shots, but spanning the set).
- API: `POST /api/repair` body `{ponds: [{name, major}], downstream: bool}` → resolves + validates the
  scope (422 + the broken pair on a disconnected set), returns the topological plan, and starts it. `/api/status` carries
  per-Pond repair state (queued / rebuilding / done / failed) so the UI can animate progress.
- UI: a **Repair** button in the Sidebar's **Failures** section enters *repair-selection mode*:
  - the DAG canvas nodes become click-to-toggle (a distinct selection rim; `store.repairMode` +
    `store.repairScope`);
  - an **Include downstream** button expands the selection to its downward closure;
  - connectivity is checked live (a broken path disables **Go** with a reason; affected-but-unselected parents warn);
  - **Go** POSTs the repair and the canvas animates the plan running through in order; **Cancel** exits.
  - Heed `frontend/AGENTS.md` (Next 16) for the interactive-mode additions to `DagCanvas`/`store`.

---

## Side effects to handle (across 2 & 3)

- **Cross-Catchment draws — nothing special needed.** A draw is a file transfer that lands the producer's
  published artifacts into the downstream Catchment's landing zone; the local Pond then reads them with the
  *same* `read_delta` window logic as any Trickle. So a refresh "just works": the draw lands the rebuilt
  main (always shipped wholesale) and the floor-bearing sidecar (already travels in the zip), and the local
  `read_delta` coverage-misses on the floor and full-reads the rebuilt main — exactly a local Trickle doing
  `SELECT *`. The incremental-draw `landed_after`/`land_windowed` transfer is just an optimization beneath
  that read; stale rows it keeps below the new floor are harmlessly ignored by the floor-aware local read
  (and reclaimed by retention). The only requirement is the one we already need anyway: the local
  `read_delta` keys coverage off the landed sidecar `floor`.
- **Egress (Spouts)**: same shape one hop further — a destination watermark below the new floor must
  re-bootstrap (full reload the destination). Not built yet (`plans/egress.md`); note for that work.
- **In-flight runs / quiesce**: both refresh and repair must terminate an in-flight run before wiping
  (kill the Duck, like the current full-refresh guidance), and clear any failed/killed fault state — a
  rebuilt Pond is a clean slate.
- **Concurrency**: one repair at a time per Catchment (a second is rejected while one is active); a
  standing trigger that fires mid-repair is deferred by the quiesce.

---

## Sequencing

1. **Deliverable 1** (bugfix) — standalone, no engine/UI, lands first; it's the correctness foundation
   the propagation in 2 & 3 stands on.
2. **The coverage floor** + **Deliverable 2** (refresh flag) — the floor mechanism, the refresh run
   semantics, CLI + the per-Pond UI toggle.
3. **Deliverable 3** (repair) — the Driver repair-plan, connectivity validation, the CLI verb, and the UI
   selection mode. Reuses the refresh-run machinery from 2.

## Open questions

- **Wipe vs. self-correct**: a comprehensive Trickle/overwrite Ripple could refresh *without* wiping
  (`previous_f=NEVER` + floor bump self-corrects the main via the comprehensive diff). Uniform wipe is
  simpler and always correct; the no-wipe optimization (cheaper, preserves changelog granularity) is a
  later refinement — confirm we start with wipe.
- **Repair failure UX**: abort-all vs. retry-from-the-failed-node vs. leave-completed-and-stop. Lean
  retry-from-here.
- **Floor representation**: a first-class field in `_trickle.json` (+ the existing Iceberg `duckstring.
  floor`), and whether `read_delta` should prefer it over `min(changelog)` always or only when present.
- **Repair quiesce granularity**: suspend only `S`, or also the immediate upstream Inlets feeding `S`
  (to stop a new epoch arriving mid-repair and racing the plan)?

## Testing

- **Bugfix**: as above (`is_full` per branch; retention-lag stale-row repro; builder bootstrap intact).
- **Refresh flag**: setting it makes the next run wipe + rebuild + bump `floor`; a downstream Trickle
  coverage-misses on that run and ends row-for-row correct (incl. a deletion that happened during the
  wrong period); the flag clears on success and persists across a Catchment restart while pending.
- **Floor**: `read_delta` falls back when `previous_f < floor` even with a non-empty changelog; retention
  and refresh both advance it.
- **Repair**: connectivity accept/reject (the diamond `{A,D}`-without-`B`/`C` rejected naming the broken pair, `{A,B,D}` accepted;
  `--downstream` accepted); topological execution order over an induced subgraph (a diamond `A→B,A→C,
  B→D,C→D` rebuilds `A` then `B`/`C` then `D`); a mid-plan failure stalls descendants; quiesce blocks a
  concurrent demand run; e2e on the demo Trickle chain (`orders→catalog→priced→revenue`) repairing
  `catalog` with `--downstream` and asserting `revenue` reflects the rebuild.
- `ruff check .` clean; the frontend selection mode covered by a component test if feasible.
