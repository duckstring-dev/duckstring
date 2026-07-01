# Deleting a table or Object from a Pond

Status: **designed, unbuilt.** The ability to remove **one** published output — a table or an
[Object](objects.md) — from a Pond *without* refreshing the whole Pond. The escape from "something looks
off, so I `rm -rf` the Pond and start over" (see the reset/undeploy discussion), scoped to a single
artifact.

## The model — one artifact, both copies

A Pond keeps every output in two places: the **registry** (`registry.duckdb`, where Ripples compute and
where a Trickle's incremental state lives) and the **published** `data/` collection (the parquet /
`__changelog` / `__base` / `__band` / sidecar a downstream reads). That pairing is **mechanistic** — the
user thinks of *one* object. So a delete **always removes both.** There is deliberately **no
"delete just the published copy"** (a re-export from the intact registry) — it is confusing and leaks the
two-copy detail.

A delete is **not** a refresh. Refresh sets `previous_f = NEVER` for the *whole* run so every Source is
re-read in full; a per-artifact delete does no such thing — it drops one collection and lets the next run
rebuild *just* that one, reading the other Sources' **current state** (not deltas). So a delete is
*cheaper* than a refresh and doesn't disturb the Pond's freshness / `previous_f`.

## Is it safe? — by artifact type

Safety tracks whether the artifact is a **stateless leaf** or an **incrementally-maintained log**.

- **Objects** — a stateless blob; nothing reconstructs from it. Delete `objects/{name}/` + the sidecar
  `objects` entry. Clean. It returns only if the producing Ripple writes it again (a train-once model
  stays gone — the point). No registry involved.

- **Overwrite tables** — recomputed wholesale every run (`export`'s `copy_to` path). Drop the registry
  table + `{name}.parquet` + the sidecar entry. If the code still produces it, the next run re-exports it
  identically; if not, it's gone. Safe.

- **Merge Trickle tables** — the subtle one, and the reason the engine needs a small change (below). The
  published main is a *differential log* read by (a) the table's own future runs and (b) downstream delta
  reads. The **incremental** write path ([`apply_zset`](../src/duckstring/trickle/io.py)) composes ΔO from
  the *Source* deltas and **appends** it to `__changelog` — it **never reads the sink**; the current state
  is reconstructed on read (`reconstruct_current` = base ⊎ changelog). So dropping the log and then feeding
  it an incremental delta yields **only that delta**, silently presented as a fresh bootstrap
  (`apply_zset` even bootstraps the floor when the changelog is absent). That is the corruption to avoid.

  The **comprehensive** path is the safe one: [`merge_table`](../src/duckstring/trickle/io.py) recomputes
  `O'` from the Sources' *current state* (`builder._full_join`, `read_table` per leaf — no `previous_f`
  dependence) and diffs it against the reconstructed prior, whose **absent case already means "empty
  prior → emit all of `O'` as `+1`"** (`merge_table`'s no-prior branch). So a dropped main + a
  comprehensive recompute = a correct full rebuild, already coded. The **only** missing piece is *routing*
  to it.

- **Aggregate Trickles** (`.aggregate`) — here the incremental path *does* read sink-side state: the
  running accumulator companions `_duckstring_agg_{name}` / `_duckstring_acc_{name}`. So the delete must
  drop those too; the same absence trigger then routes the rebuild through `_agg_rebuild` (wholesale)
  rather than folding deltas into stale accumulators.

- **Append Trickles** — either the append is derived comprehensively (the builder `.append()` path
  re-derives it from current state and append-filters against the now-empty history → re-appends
  everything) **or** it is hand-rolled to append only new rows against the target/`previous_f` window, in
  which case a delete is a *deliberate history drop* that won't cheaply rebuild (re-deriving its history
  needs full Source-history reads). That is the user's call — gate it behind an **extra warning**.

## The engine change — absence ⇒ comprehensive (one trigger, also a safety net)

Today the builder chooses incremental-vs-comprehensive purely from *Source* fullness
([`builder._compute`](../src/duckstring/trickle/builder.py) → `state.is_full`); it never considers the
output. Add: **force the comprehensive path when the output Trickle has no registry meta entry.**

```python
# builder._compute(...), before the incremental return:
absent = name not in trickle.read_meta(self.ctx.con)
if not ivm or absent:
    o_prime = self._full_join()
    self._require_pk(out_pk, o_prime.columns)
    return "comprehensive", o_prime
```

- The check is **`name not in read_meta`**, *not* "the base table doesn't exist" — a young merge Trickle
  legitimately has only a changelog (no base pre-checkpoint), so a raw table-existence test would
  spuriously recompute every run. The meta row is exactly what the delete removes, and it's the same
  condition a genuine bootstrap satisfies (where Sources are `is_full` anyway → no behaviour change / no
  perf regression on normal runs).
- It covers **merge, aggregate, and builder-append** uniformly — all route through `_compute(name, …)`.
- Direct **`pond.merge_table(full_state, pk)`** already handles absence (empty-prior branch) — no change.
- It is a **sound invariant regardless of deletes**: applying an incremental delta onto a nonexistent log
  is *never* correct, so detecting it and degrading to full is the right defensive behaviour (guards
  against a registry the operator/OS half-clobbered, too).

Optional belt-and-braces: have `apply_zset` **raise** if handed an incremental delta for a changelog that
doesn't exist (the builder never does this once the trigger lands, so it's a pure assertion).

## The delete mechanism

**Registry side is Duck-owned**, so a table delete is Duck-mediated at a run boundary (the proven
`refresh` shape), and **not** `previous_f=NEVER`:

1. **`executor.wipe_table(name)`** (sibling of [`executor.wipe`](../src/duckstring/duck/executor.py)) —
   drop the whole registry collection for `name`: the base/main table, `{name}__changelog`, `{name}__band`,
   `{name}__droplog`, `_duckstring_agg_{name}`, `_duckstring_acc_{name}`, **and** the `_duckstring_trickle`
   meta row + floor (a new `trickle_io.drop_meta(con, name)`). For an overwrite table it's just the one
   table + meta. Uses the `trickle_io` naming helpers so it stays in sync with the collection shape.
2. **`dataplane.unpublish_table(data_dir, name)`** — remove the published collection: `{name}.parquet`,
   `{name}/`, `{name}__changelog/`, `{name}__band/`, `{name}__base/`, `{name}__droplog/`, and the `name`
   entry from the sidecar. Storage-seam ops (`remove`/`rmtree`), so local + object store.
3. **Flag + rebuild.** A persisted pending-drop (a small `pond_pending_drop(pond_id, table_name)` table,
   like `pond_window`) survives a Catchment restart; it rides the `BeginRun` job the way `refresh` does.
   The delete issues a **force** so a run happens promptly: at run start the executor drops the registry
   collection (1) + unpublishes (2), then the run proceeds. The builder hits the absence trigger →
   comprehensive → rebuilds `name` from current Source state → `export` republishes it. Other tables
   re-run under the force but, their logs intact, produce empty deltas (no-ops). **Cost ≈ one
   `_full_join` of the deleted table**, not a Pond refresh.
   - If the code **no longer produces** `name`, the run simply doesn't rebuild it → permanently gone. Same
     verb, both outcomes: *"re-derive the Pond's outputs; `name` returns iff it's still an output."*
4. **Downstream** self-heals like refresh: the rebuild raises `name`'s floor (bootstrap), so a downstream
   consumer coverage-misses and full-reads on its next run. Force doesn't advance this Pond's freshness
   (the data didn't get *fresher*, just rebuilt), so propagation is lazy/honest — identical to refresh.
   Use `repair` if you want a connected, topologically-sequenced rebuild instead.

**Objects skip all of this** — no registry, so the Catchment removes `objects/{name}/` + the sidecar
`objects` entry directly (a `objects.delete_object(data_dir, name)` helper). Gate on the Pond being
**idle** so it can't race a run's `commit_objects` re-adding the entry (a mid-run delete would otherwise be
undone by the commit). No run needed; rebuild only if you re-run a Ripple that writes it.

## Surfaces

- **API** (both `dependencies=[auth.full]` — destructive):
  `DELETE /api/ponds/{name}/tables/{table}` and `DELETE /api/ponds/{name}/objects/{obj}`
  (`major`/`version` query params, resolved by `Driver.resolve`).
- **CLI**: `duckstring delete-table {pond} {table}` and `duckstring delete-object {pond} {name}`
  (`--major`/`--version`; a confirmation prompt, `--yes` to skip). The append warning fires here for an
  append-mode table.
- **Data Viewer**: a **Delete** action per row on the Tables tab and the Objects tab (the "Drop" button
  the earlier reset discussion anticipated) → the two `DELETE` routes. A destructive-confirm dialog:
  tables say "drops and rebuilds `{table}`" (append tables add the history-loss warning); objects say
  "removed unless a Ripple writes it again". Full access only, matching the auth gate.

## Guards

- A table delete **always** drops the registry (never a published-only delete) — enforced by routing
  everything through `wipe_table` + `unpublish_table` together.
- **Append warning** — deleting an append-mode Trickle warns that history is dropped and rebuilds only if
  produced comprehensively (we can't statically tell hand-rolled from builder appends, so warn on the
  mode). Surfaced in the CLI prompt + the viewer dialog.
- **Idle gating** for objects (avoid the `commit_objects` race); tables are inherently run-boundary-safe.

## Deliberately out of scope

- **No re-export / "delete just the published copy"** — confusing; a delete drops both copies.
- **No row-level / predicate deletes** — this removes a whole named artifact, not rows within one.
- **No cross-Pond cascade** — deleting a table a downstream still consumes lets that downstream
  coverage-miss and reload; deleting a *shared* upstream on purpose is a `repair`-shaped operation, not a
  single delete.

## Build order

1. The engine trigger in `builder._compute` (`name not in read_meta` ⇒ comprehensive) + `drop_meta`, and
   the optional `apply_zset` assertion. This is the correctness core; land + test it first, standalone.
2. `executor.wipe_table` + `dataplane.unpublish_table` + `objects.delete_object`.
3. The pending-drop flag (`pond_pending_drop`, migration) + carrying it on `BeginRun` + the force-rebuild
   (Driver, mirroring `refresh`).
4. API routes + CLI + the Data Viewer Delete actions + the append warning.

## Tests

- **Trigger**: a merge Trickle whose registry collection is dropped rebuilds *correctly* on the next run
  (full state, not just the latest delta) — the core regression against silent corruption.
- Overwrite table delete → re-exported next run; delete of a no-longer-produced table → stays gone.
- Aggregate delete drops the accumulator companions and rebuilds via `_agg_rebuild`.
- Append delete warns; a builder-append rebuilds, a hand-rolled append delta drops history (documented).
- Object delete removes the dir + sidecar entry; idle-gating prevents the commit race.
- Downstream coverage-miss + reload after an upstream table rebuild.
- API/CLI auth gating (full only); `major`/`version` resolution.
