# Trickle: incremental I/O and transfer (not incremental compute)

Status: **designed, unbuilt**. The full design from the bed-down sessions. Builds on the Iceberg data
plane in `data-plane-iceberg.md` — Trickle **requires the deferred Iceberg backend** (snapshots,
schema metadata, the `_duckstring_*` namespace, `pond.previous_f`, the mode-capable `DataPlane`
interface in `src/duckstring/dataplane.py`). Build that first.

## Scope — what Trickle is and is not

A **Trickle** is a Ripple variant for **incremental** work. Requirements:
- **Tabular** output (Arrow-compatible).
- A **declared primary key** per output table (identity for merge + downstream consumption).
- Rows in its incremental structures are stamped with **`_duckstring_f`** (the run's freshness).

**Trickle delivers incremental I/O and incremental transfer, NOT incremental computation.** Joins
recompute fully — you cannot derive `Δ(A⋈B)` from input deltas without IVM (the delta-join needs each
delta against the *full* other side), and IVM would force the relational algebra (and a uniform compute
layer like Ibis) into the core. So Trickle's win is a **small delta out** (small writes, small draws),
not less work in. The honest scope statement to keep in the docs.

**Composition: incremental chains Trickle→Trickle only.** A windowed delta read needs the source to
preserve change history; a Trickle reading a Ripple (overwrite) source falls back to a full read, and a
Ripple reading a Trickle source reads its clean current state. Mixing an overwrite node into a chain
full-materialises at that hop.

## Settled principles (recap)

- **Ripple = overwrite; Trickle = history-preserving** (append or merge). Binary; no "append-then-
  compact" middle (that collapses to overwrite — see data-plane plan).
- **`_duckstring_f` lives in the data**, read as a **content predicate**, never a snapshot cursor — so
  it works regardless of compaction and dodges pyiceberg's immature incremental-snapshot-scan API.
- **System columns, reserved `_duckstring_*` namespace:** `_duckstring_f` (freshness stamp),
  `_duckstring_op` (`upsert`/`delete`, in the changelog), `_duckstring_hash` (change-detection digest,
  in the merge main). The whole prefix is reserved + rejected at publish (already enforced).
- **Incremental read is the window `(previous_f, f]`** — both bounds from the consumer's own freshness
  (`pond.previous_f`, `pond.f`), **no per-edge watermark**. The upper bound `f` is the **exactly-once
  ceiling**: it stops a consumer re-reading rows from a source that independently ran ahead of the
  consumer's coordination epoch. Windowing is definitional to a delta read; full reads can't over-read.

## Write API

```
append_table(name, relation)                                   # insert-only
merge_table(name, relation, *, comprehensive=True, deletes=None)  # upsert (+ delete)
```

### `append_table` — insert-only, trust-the-writer fast path
- Strictly append. **No PK uniqueness check, no diff, no change detection** — performance path for
  event/fact logs whose identity is unique by construction.
- No `_duckstring_op`, no `_duckstring_hash`, no deletes. Each row carries `_duckstring_f`.
- **One table**, append-only: it is simultaneously the history, the full-read source, and the delta
  source (see Storage). The earlier "also write a separate delta" idea is redundant for append — its
  history *is* its delta.

### `merge_table` — upsert, with auto change-detection by default
- **`comprehensive=True`** (default): the relation is the **complete current state**. Duckstring
  **diffs it against the prior state** (via `_duckstring_hash`) to derive inserts / updates / deletes
  automatically. `deletes` is rejected (redundant). Correct-by-construction for any computation,
  including joins. Cost: full recompute + a diff per run (the I/O is incremental, the compute is not).
- **`comprehensive=False`** (expert path): the relation is a **partial** set of changed rows. Diff only
  the supplied rows against their current versions (a semi-join probing just those PKs — cheaper),
  leave untouched PKs alone, and take deletes **only** from the explicit `deletes` PK set.
- **Failure-mode asymmetry (document loudly):** over-merge (re-emitting unchanged rows) is *safe* —
  idempotent merge absorbs it, just churn. **Under-merge** (missing a changed row; or under-supplying
  `deletes`) is **silent data corruption**. So `comprehensive=True` is the safe default; the partial
  path puts that risk on the developer explicitly.

The spectrum, fastest→safest: `append_table` → `merge_table(comprehensive=False, deletes=…)` →
`merge_table(comprehensive=True)`.

## Storage layout

Mode + PK are recorded as **Iceberg table properties** (`duckstring.mode`, `duckstring.pk`) so they
**travel with the table** (a cross-Catchment draw has no access to the producer's `duck.db`). Mirror
into `duck.db`/`pond_version_schema` for local queries; the table is source of truth.

### append Trickle — one table
- Append-only, `_duckstring_f` per row. Full read = scan all; `source.delta` = window predicate.
- **Compaction:** file-compact the **cold** tail, **keep the recent runs granular** (their files are
  `_duckstring_f`-homogeneous, so the window read prunes exactly via Iceberg manifest stats). Retention
  is an optional TTL only.

### merge Trickle — main table + changelog table
- **main**: the **clean current state** — one row per PK, **present ⇒ active, no tombstones, never a
  read-time filter**. Carries `_duckstring_hash` (for the diff). Freshness is at the **snapshot** level
  (Phase-1 `f` stamp), not per-row. For full reads + bootstrap + the too-far-behind fallback.
  - `comprehensive=True`: **overwrite** the main with the full output — reuses the Phase-1 overwrite
    mechanism, so the main needs **no CoW-upsert and no Iceberg delete-files** (dodges the immature
    path entirely). The diff is computed *before* the overwrite (new output vs the prior snapshot).
  - `comprehensive=False`: **upsert** the main (apply supplied changes + drop `deletes`). This is the
    only path needing a real CoW-upsert on the main.
- **changelog**: append-only **CDC stream** — `(_duckstring_op, pk, <row cols>, _duckstring_f)`,
  **changed rows only**, deletes as `op='delete'` (PK populated, cols null). This is *not* SCD-2 (no
  validity intervals). It is the single home for deletes — which does **not** reintroduce the tombstone
  footgun, because the footgun was tombstones in the *current-state* table read by `SELECT *`; the
  changelog is explicitly an op stream consumed by machinery, never "the active rows."
  - **Rolling retention** (see below) bounds it to the lag window; it holds only changed rows, so size
    ≈ churn-per-run × retained-runs (not all-time).

## Change detection (the `comprehensive` diff)

- Store **`_duckstring_hash`** (64-bit non-crypto, e.g. DuckDB `hash()` over the non-PK columns in
  schema order) in the main. The diff reads only `(pk, _duckstring_hash)` from the main (narrow) and
  materialises full content only for changed rows.
- **Collision-safe:** comparison is *per-PK* (new hash vs old hash for the same key) → P ≈ 2⁻⁶⁴ per
  changed row; the birthday bound does **not** apply (no cross-row pairwise compare).
- **Caveats:** a schema change re-hashes every row → a one-time full re-emit (correct, but a thundering
  transfer on a schema bump). Canonicalise types/nulls/floats/column-order so the hash is stable run to
  run otherwise.
- Diff result → inserts (PK absent in old), updates (hash differs), deletes (PK absent in new) →
  appended to the changelog with `_duckstring_f = F`; main overwritten (comprehensive) or upserted
  (partial).

## `source.delta` semantics

`source.delta` resolves the source's declared `duckstring.mode` and reads transparently over the
window `(pond.previous_f, pond.f]`:
- **append source**: window-filter the single table (Iceberg manifest stats prune to the granular
  recent files; no scan).
- **merge source**: read the **changelog** window, then **collapse per PK to the max-`_duckstring_f`
  row** (`QUALIFY row_number() OVER (PARTITION BY pk ORDER BY _duckstring_f DESC) = 1`) → the **net**
  change per key (an upsert row, or a delete marker if the latest windowed op is a delete).
- **Coverage check / fallback:** if `previous_f` < the changelog's oldest-retained `_duckstring_f`
  watermark, the window isn't fully covered → **fall back to a full read** of the main (then resume
  incrementally). Bootstrap (`previous_f = NEVER`) is always a full read.
- **`source.delta` always targets `_duckstring_f`-homogeneous (granular) files** — the changelog / the
  append hot region — never the mixed-`f` history; manifest-stat pruning is scan-free and conservative
  (never drops in-window rows).

## Consumer merge contract

A Trickle consuming a Trickle source merges `source.delta` into its own state **by PK with max-
`_duckstring_f` resolution**: union the windowed upserts and deletes, group by PK, apply the latest op.
This handles **delete-then-re-add** (`upsert@8` beats `delete@5` → present) and same-`f` is impossible
by construction (a PK is upsert *or* delete in one run; partial-mode validation rejects supplying both).

Over-read is **idempotent-safe** for merge consumers (re-applying the same latest rows). Union/append
consumers rely on the exactly-once ceiling; append sources preserve history, so the ceiling holds.

## Helpers for the partial path (`comprehensive=False`)

The IVM-like skeleton of a partial merge — *find the affected output keys, recompute only those, derive
the deletes* — is generic, and we can give it framework-agnostic helpers **without** imposing a compute
layer (Ibis et al.) by having them operate on **key-set relations only**. The transform stays raw SQL /
the DuckDB relation API; the helpers do the set bookkeeping. Three primitives:

- **`delta.keys()`** — a source delta's changed keys (`upserts ∪ deletes`), as a relation of its PK.
- **`pond.keys_joining(spine, delta, on=…)`** — spine PKs whose `on` column(s) match `delta.keys()`,
  i.e. *which output keys a change in this dimension ripples to*. Reads the full spine + the delta's
  keys; returns spine PKs.
- **`affected.dropped(recomputed)`** — deletes = `affected EXCEPT recomputed[pk]`. A single set
  difference, and **correct however `affected` was built**: because `.keys()` folds in source deletes,
  a deleted spine key is in `affected`, never in `recomputed`, so it lands in the delete set
  automatically — which is exactly the subtle case that's easy to miss by hand.

```python
@trickle(pk=("order_id", "line_no"))
def priced_line(pond):
    ol = pond.read_delta("sales.order_line")
    pr = pond.read_delta("catalog.product")
    pond.read_table("sales.order_line")          # view `order_line` (full)
    pond.read_table("catalog.product")           # view `product`    (full)

    affected = ol.keys().union(pond.keys_joining("sales.order_line", pr, on="product_id"))
    affected.create_view("affected")

    recomputed = pond.con.sql("""
        SELECT ol.order_id, ol.line_no, ol.product_id, p.name AS product_name,
               ol.qty, p.unit_price, ol.qty * p.unit_price AS line_total
        FROM order_line ol JOIN affected USING (order_id, line_no)
        JOIN product    p USING (product_id)
    """)

    pond.merge_table("priced_line", recomputed,
                     comprehensive=False, deletes=affected.dropped(recomputed))
```

**`keys_joining` join constraint:** `on` equi-joins the spine's column(s) to the **delta source's full
PK** — the delta side is pinned to its PK (composite is fine; arity = the PK's), the spine side is any
column(s) (typically an FK, *not* required to be the spine's PK). The delta-side-must-be-PK rule is
what makes **delete propagation sound**: a delete tombstone carries only the PK, so a non-PK delta-side
join could propagate upserts but would silently drop deletions. Reject non-PK delta-side joins.

**Where it's clean vs. where it gets weird:**
- **Star / enrichment** (one spine owns the output PK, dimensions joined directly): clean — one
  `keys_joining` term per dimension edge, unioned. The dominant Trickle shape; scales fine.
- **Snowflake / transitive chains** (a change two hops away ripples through an intermediate): expressible
  by *nesting* `keys_joining`, but it gets hairy — you're hand-traversing the FK graph again (example-2
  territory). Cap the helpers at declared direct edges; deeper chains drop to hand-rolling or
  `comprehensive=True`. Going further is the slope toward reimplementing a query planner.
- **Aggregations** want a *sibling* primitive, not this one: `pond.affected_groups(delta, by=…)` (group
  keys touched by the delta), then recompute by re-aggregating those groups from the full input.
- **Window functions, non-equi / self-joins:** no clean key propagation → `comprehensive=True` or
  hand-rolled.

**Residual risk:** the helpers shrink the boilerplate and structurally guide the error-prone parts, but
they do **not** remove the partial path's core obligation — you must still enumerate *every* edge that
can ripple to the spine. Miss a `keys_joining` term and you under-merge, silently. So `comprehensive=True`
stays the default (it has no enumerate-every-edge obligation); these make the opt-in nicer, not safe.

### Builder: `pond.trickle(...)` — optional sugar over the helpers

A fluent builder for the common shapes:

```python
priced_line = (
    pond.trickle("sales.order_line")
        .join(pond.trickle("catalog.product"), on="product_id")
)
priced_line.merge()
```

- It records a **tiny op graph** (`Source` / `Join` / `Filter` / `Project` [/ `GroupBy`]) over Trickle
  sources — **its own minimal IR, not Ibis** (Ibis is only needed for the *general* arbitrary-algebra
  version we're not building; a closed op set is cheaper to model ourselves and maps straight onto
  DuckDB relations). `.merge()` walks the graph: `read_delta` each leaf, propagate affected keys along
  the recorded edges, recompute the affected slice, `merge_table(comprehensive=False, deletes=…)`.
- **Correct-by-construction within its op set.** Because it sees the whole graph, it can't *forget* an
  edge the way hand-composed `keys_joining` can — no silent under-merge. That safety, not just the
  terser API, is the reason to prefer it for supported shapes.
- **Closed op set; hard error outside it.** An unsupported operation (non-equi/self/cross join, window,
  `having`, opaque expr, a non-PK join key, …) **raises at build time** — it does **not** silently
  degrade to a full refresh. Silent degradation hides a performance cliff (you think you're
  incremental; you're secretly recomputing everything every run), so the builder refuses rather than
  surprise you.
- **Escape hatch: a downstream ripple.** Do the unsupported/complex computation in a *subsequent*
  Ripple/Trickle consuming the builder's output. (Caps incrementality at that hop unless the downstream
  is itself a comprehensive Trickle — Trickle→Ripple full-materialises; see composition rules. Often
  the right shape anyway: keep the incremental enrichment in the builder, the gnarly bit downstream.)
- **Sequencing:** build the helpers first (primitives, no DSL ceiling, usable standalone); the builder
  is optional sugar layered on them. Keep its op set deliberately small and let the downstream-ripple
  escape carry the rest — don't let it grow into a parallel transform DSL chasing SQL's surface.

## Retention, compaction, idempotency

- **Retention = a lag SLA.** Default `retain_t ≈ 30 days` ("a consumer/draw offline this long resumes
  incrementally; longer → automatic full re-read"), with an optional `retain_n` count cap. Time-based
  scales with run frequency. **Correctness never depends on retention** (full-read fallback covers it);
  longer/forever is the opt-in for audit/replay.
- **Cleanup at write time**, by **dropping whole expired files** (changelog files are `_duckstring_f`-
  homogeneous → "remove files whose max `_duckstring_f` < cutoff" is metadata-only, no rewrite). Update
  the **oldest-retained-`_duckstring_f` watermark** for the coverage check.
- **Three distinct operations — don't conflate:** file-compaction (#1, tidiness, history-preserving),
  state-collapse (#2, N/A — handled by the CoW/overwrite main), expiry (#3, the retention above). Also
  run `expire_snapshots` on the main for Iceberg metadata bloat (separate from changelog row retention).
- **Idempotency** (retry/replay at the same `f`): before writing run `F`, delete rows where
  `_duckstring_f = F` from the changelog (and the append table), then re-append; the main overwrite is
  inherently idempotent. Keyed on `F`, hits only the newest files.

## Draws (incremental transfer) — `routes/draw.py`, `poller.py`

The data-plane plan left the draw at get-all; Trickle implements incremental transfer here:
- **merge source**: ship the **changelog window** (`_duckstring_f` in the consumer's window); the
  consuming Catchment's `source.delta` collapses + merges. Bootstrap/fallback ships the main (full).
- **append source**: ship the single table's window.
- The consumer sends its `end_f` (the protocol slot reserved in Phase 1); the producer serves the
  windowed rows. Beyond retention → full transfer of the main.

## Non-goals / explicitly out

- **Incremental computation / IVM** — joins recompute fully; the win is delta-out, not work-in.
- **Formal SCD-2** — the changelog is a CDC op stream, no validity intervals.
- **Iceberg equality-delete / merge-on-read for the main** — avoided by overwriting (comprehensive) or
  CoW-upserting (partial) the clean main + an append-only changelog.

## Future & prior art — automatic IVM (deliberately not building in-core)

We explored a general automatic-IVM layer (trace affected keys/deltas through arbitrary relational
algebra, fall back on anything unsupported). Conclusion: **don't build it.** It's a real IVM engine
with the worst failure mode in data — *silent* wrong results — and it's an entire industry.

- **Over-approximate IVM** (trace affected keys → recompute the affected slice → diff) is what the
  helpers/builder do: in-process, DuckDB-only, bounded to a small op set. This is our supported
  incremental story, and at the **~50M single-node scope it's almost certainly enough.**
- **Exact IVM** (maintain per-operator deltas/state, never recompute from base) is service- or
  Rust-shaped: **Feldera/DBSP, Materialize, RisingWave** (streaming DBs you connect to), **pg_ivm**
  (Postgres), **differential-dataflow / `dbsp`** (Rust), **DBToaster** (SQL→native codegen). There is
  **no mature pip-install, pure-Python, in-process** exact-IVM engine; the closest embedded-Python
  option is **Bytewax** (timely-based, stateful), but it's stream-shaped dataflow primitives, not
  turnkey relational IVM. So "import a library and go" doesn't exist for exact IVM — the engines are
  the escalation for past-single-node, and they're service-shaped.

### Integrating an external IVM engine (when you outgrow in-process)

The Trickle contract already speaks the right interface — **change-set in (`read_delta`), change-set
out (`merge_table(comprehensive=False, deletes=…)`), `pond.f`/`previous_f` as the watermark** — so a
stateful engine slots in as ordinary ripple code: feed each source's delta, take the engine's emitted
output delta, `merge_table` it; bootstrap (first run / lost state / freshness mismatch) feeds full
`read_table` → comprehensive merge.

- **Consistency burden the author owns:** the engine's state lives outside the data plane, so
  **checkpoint keyed by `f`** and **restore from `previous_f`** at the top of each run — that's what
  makes a *stateful* engine replay-stable under Duckstring's "re-run at the same `f`" model (feeding
  deltas isn't idempotent; restoring the pre-run checkpoint and re-feeding is). Bootstrap on any
  checkpoint mismatch/loss.
- **Affordance to add: `pond.state_dir`** — a managed, per-pond, writable directory that survives
  across runs, is included in `catchment archive`, and survives a redeploy (same durability as the
  Iceberg data). The one piece of plumbing an embedded engine needs; worth reserving even before use.

## Open questions for the build session

- `comprehensive=False` main upsert: pyiceberg `upsert` maturity vs. a self-computed delete+append
  (we know the changed PK set from the caller).
- `source.delta` return shape: leaning to a **`Delta` with `.upserts` / `.deletes` / `.keys()`**
  relations (the pair form reads far better than an `_duckstring_op` column for the partial-path
  helpers above) — confirm against the `_duckstring_op` alternative.
- Helper homes/signatures: `Delta.keys()`, `pond.keys_joining(spine, delta, on=…)` (reject non-PK
  delta-side joins), `affected.dropped(recomputed)`, and the aggregation sibling
  `pond.affected_groups(delta, by=…)`.
- Hash canonicalisation specifics (decimal/float/null/timestamp normalisation, nested types).
- PK + mode declaration surface: `@trickle(pk=…, mode=…)` decorator vs. per-`write` args, and how it
  feeds Phase-2 schema/contract capture (`pond_version_schema` already earmarked a `primary_key` slot).
- Where `source.delta` lives on the `Pond` handle and how the window bounds are injected (mirrors how
  `pond.f` / `pond.previous_f` are threaded through the executor).
- Builder (`pond.trickle`): the exact closed op set to support first (lean: `Source`/`Join`/`Filter`/
  `Project`, maybe `GroupBy`), the minimal IR shape, and the build-time error surface for unsupported
  ops. Layer on the helpers; ship after them.
- `pond.state_dir` (only if/when external-engine integration is pursued): a managed per-pond writable
  dir, archived + redeploy-surviving. Reserve the name; no need to build until needed.

## Testing

- Append: window read returns exactly the runs in `(prev, f]`; manifest pruning skips cold files;
  idempotent replay at the same `f`.
- Merge comprehensive: diff detects insert/update/delete correctly; main is clean (no tombstones);
  changelog carries the ops; over-read is idempotent; delete-then-re-add resolves to present.
- Merge partial: supplied upserts applied, untouched PKs untouched, explicit deletes honoured;
  under-supplied deletes leave stale rows (documented risk, asserted in a test so it's intentional).
- Partial helpers: `keys_joining` propagates a dimension's upserts *and* deletes to spine keys (and
  rejects a non-PK delta-side join); `affected.dropped` deletes the keys that fell out of the recompute
  (incl. source-deleted keys); the worked star example equals a `comprehensive=True` run row-for-row.
- Builder: a supported `join`/`filter`/`project` chain produces the same result as the equivalent
  comprehensive Trickle, incrementally; an unsupported op **raises at build time** (no silent
  full-refresh); it can't under-merge (the graph supplies every edge).
- `source.delta` collapse = max-`f`-per-PK; coverage-miss falls back to full read; bootstrap full-reads.
- Retention: at-write file drop past `retain_t`; oldest-retained watermark advances; a consumer behind
  it full-reads. Cross-Catchment draw transfers only the window; bootstrap transfers the main.
- `ruff check .` clean; e2e on a demo Trickle (extend `test_runtime`).
