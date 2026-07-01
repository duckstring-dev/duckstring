# Reset — a clean slate for a Pond or the whole Catchment

Status: **designed, unbuilt.** The sanctioned replacement for "something feels weird with the `.duckstring`
files, so I `rm -rf` it and start over." Two scopes:

- **`reset {pond}`** — scrub one Pond's runtime state and rebuild it from scratch, keeping its deployed
  code and operational config.
- **`catchment reset`** — do that for every Pond at once: the whole runtime back to a fresh-deploy state,
  keeping the deployed bundles, operational config, secrets, and keys. The honest fix after a package
  upgrade left on-disk state in a stale format.

## Why it's a distinct verb (vs delete / refresh / repair)

We already have the neighbours; reset is the one that scrubs *everything* and rebuilds:

| verb | scope | clears | rebuilds | keeps |
|---|---|---|---|---|
| **delete** | one table/Object | that artifact (registry + published) | no — stays gone (returns on next genuine run) | all other state |
| **refresh** (`refresh_pond`) | one Pond | registry (lazily, at next run) | yes, cold, at the next natural run | data-dir orphans, ledger, history, freshness |
| **repair** (`Driver.repair`) | a connected set | registry (force+refresh now) | yes, cold, topologically now | data-dir orphans, ledger, history |
| **reset** (this) | one Pond / all | **registry + whole data dir (incl. orphans + Objects) + ledger + freshness/fault/demand state + optionally run history** | yes, cold | **deployed artifact + operational config** |

The gap reset fills over refresh/repair: those rebuild the *current* tables but leave **orphans** (published
tables/Objects the code no longer produces), the **ledger** (`pond.db`), and **run history** behind — the
detritus that makes a Pond "feel weird" after code churn or a package bump. Reset is the total scrub.

## Kept vs cleared

**Always kept** (this is what makes it a reset, not an undeploy):

- The deployed **artifact** — `ponds/{name}/{version}/` + the `pond_name` / `pond_version` / `pond` /
  `pond_to_pond` identity + topology rows.
- **Operational config** — `pond_trigger` (standing Wave/Tide), `pond_window`, `pond_spout`, `pond_retry`
  (budgets), `alert_channel`. Survives, like it survives a redeploy.
- Catchment-level: `secrets.json`, `config.toml`, `catchment_key` / `catchment_meta` (keys + the Duck
  token), registrations.

**Cleared** (a Pond line = `ponds/{name}/m{major}/`):

- `registry.duckdb` (all tables + Trickle state).
- `data/` — the whole published dir: every table collection, all Objects, the sidecar, the Iceberg catalog.
- `pond.db` (the Duck's ledger).
- `duck.db` runtime rows for the Pond: `pond_state` (freshness/fault/demand → fresh-deploy defaults),
  `pond_target` (demand set).
- **Optional** (`--clear-history`, default keep): `pond_run` / `ripple_run`. History is an audit trail;
  default to keeping it. (`pond_version_schema` — the captured additive contract — is **kept**: a reset
  shouldn't lower the major line's contract high-water. Flag if that proves surprising.)

## The freshness problem — and why the two scopes differ

Freshness is monotone; you cannot rewind one Pond behind its downstream. This forces different handling:

- **Per-Pond reset must rebuild *forward*.** It cold-rebuilds via the **refresh** path
  (`previous_f = NEVER` for Source *reads* → full reads + re-bootstrap → the floor rises so downstream
  coverage-misses and reloads), but the Pond's own freshness stays put. It does **not** set the Pond to
  `NEVER` — that would strand every downstream consumer ahead of a dataless Source. Because a cold rebuild
  needs a run, per-Pond reset **forces** it (like `repair`), so the data is back before anything reads it.

- **Catchment reset *can* rewind to `NEVER`,** because *everything* rewinds together — a consistent
  fresh-deploy state, no partial-backwards edge. It resets every `pond_state` to the initial
  (`start_f=end_f=changed_f=NEVER`, no demand, no fault), then the pipeline rebuilds from the Inlets down
  on the next demand. Standing Wave/Tide triggers (kept) re-drive it automatically; a pull-only Catchment
  waits for a Tap, or `catchment reset --rebuild` injects demand at the Outlets (a topological repair).

## Orphan clearing without a data gap

Clearing `data/` *before* the rebuild leaves a window where the Pond has no published data. Two ways to
avoid a downstream read hitting the gap:

1. **Post-rebuild prune (recommended).** Reuse refresh's contract — "the published snapshot is untouched
   until the rebuild re-exports" — then, *after* the rebuild's export writes the fresh sidecar, prune every
   `data/` table/Object **not in that sidecar** (via `unpublish_table` / `delete_object`). Last-good is
   preserved throughout; only genuine orphans are removed. This is the delete machinery, driven by a diff
   against the new publish set.
2. **Eager clear at the Duck** (simpler, but a gap): clear `data/` at run start alongside the registry
   wipe (like refresh's `executor.wipe`), accepting a brief dataless window during the rebuild — same risk
   profile as refresh, just more thorough.

Recommend (1) for per-Pond reset. Catchment reset rewinds to `NEVER` so there's no gap to guard — a
downstream can't read a Source that isn't fresher than it.

## Mechanism

### Per-Pond reset (`Driver.reset_pond`)

Builds on delete's drop primitives + refresh's cold-rebuild:

1. **Idle-gate / quiesce** — reject if a Run is in flight, or quiesce it like `repair` (terminate the Duck,
   abandon the in-flight run's phantom `start_f → end_f`).
2. **Terminate the Duck** (free `registry.duckdb`), delete the registry + `pond.db` ledger files.
3. **Reset `duck.db` runtime rows** — `pond_state` fault/demand fields cleared (reuse `_clear_halt` +
   fresh-deploy defaults, keep freshness monotone), `pond_target` emptied; optionally clear history.
4. **Cold rebuild** — flag `refresh_pending` + `force` (i.e. `repair_pond` for this one Pond) so the next
   dispatch rebuilds it now, `previous_f = NEVER`, floor raised.
5. **Post-rebuild prune** — on the rebuild's `run_completed`, diff `data/` against the new sidecar and
   `unpublish` the orphans + drop orphan Objects.
6. **Downstream** self-heals off the raised floor (coverage-miss → reload), exactly as refresh already does.
   `--downstream` extends the scope (a topological repair over the connected set — reuse `Driver.repair`'s
   sequencing) when you want the subtree scrubbed together.

### Catchment reset (`Driver.reset_catchment`)

1. **Quiesce all** — terminate every Duck, cancel pending jobs.
2. **Per line, clear** `registry.duckdb` + `data/` + `pond.db` (a filesystem sweep of `ponds/{name}/m*/`,
   keeping the `ponds/{name}/{version}/` artifact dirs).
3. **Reset `duck.db` runtime** — every `pond_state` → initial (`NEVER`, no demand, no fault); clear
   `pond_target`; optionally clear `pond_run`/`ripple_run`. Keep all topology + operational-config tables.
4. **Reload** engine state from the scrubbed `duck.db` (`Driver.reload`) — a fresh-deploy engine.
5. **Rebuild** lazily (standing triggers + demand rebuild from Inlets down) or, with `--rebuild`, eagerly
   (a Tap/repair at the Outlets to pull the whole graph).

Draws and Spouts (they're real Ponds): a reset clears a Draw's landed data (it re-draws from upstream) and
a Spout's in-destination watermark is **not** ours to reset — a Spout reset re-delivers from scratch
(`resync`), which is already a verb; note the overlap.

## The package-upgrade case

`catchment reset` is its home: the old package may have written registries/ledgers/data in a format the new
package's readers reject ("feels weird"). Reset drops all of that runtime state and rebuilds it with the new
code — the honest, blunt fix. (A *migrating* upgrade that preserves data is a separate, per-format concern;
reset is the "I don't care, rebuild it" hammer. It's cheaper than `rm -rf` because it keeps deploys +
config + secrets, so you don't re-deploy or re-wire anything.)

## Surfaces

- **CLI**: `duckstring reset {pond} [--downstream] [--clear-history] [-y]` and
  `duckstring catchment reset [--rebuild] [--clear-history] [-y]`. Destructive → a confirmation prompt
  (with `--yes` to skip); the Pond form warns it rebuilds from scratch, the Catchment form spells out the
  blast radius (N Ponds, keeps deploys/config/secrets).
- **API** (both `dependencies=[auth.full]`): `POST /api/ponds/{name}/reset` and
  `POST /api/catchment/reset` (bodies for the flags). 409 if a targeted Pond is mid-run and not quiescible.
- **UI**: a per-Pond **Reset** action in the Sidebar's Control set (beside Force/Refresh/Kill) and a
  catchment-wide **Reset** under the `ControlsPanel` (beside Secrets/Alerts, full-only), each behind the
  themed `ConfirmDialog` with the blast radius spelled out.

## Relationship to undeploy (out of scope, the sibling)

Reset **keeps** the Pond (artifact + identity + config) and scrubs runtime. **Undeploy** removes the Pond
entirely (artifact + identity + config + all state) — the thing the user also asked for early on. It shares
reset's clearing step but additionally drops the `pond_name`/`pond_version`/`pond`/`pond_to_pond` rows + the
`ponds/{name}/` tree + operational config, and must handle downstream that still pins it. Plan separately.

## Open decisions

1. **Per-Pond rebuild: eager (recommended) vs lazy.** A total scrub with no rebuild leaves the Pond
   dataless and (pull-only) potentially unreachable until re-triggered. Recommend eager (force, like
   repair). Catchment reset is lazy by default (`--rebuild` to force).
2. **Orphan clearing: post-rebuild prune (recommended) vs eager clear.** Prune preserves last-good; eager
   is simpler but opens a gap. See above.
3. **History**: keep by default (`--clear-history` to wipe). Contract schema (`pond_version_schema`): keep.
4. **Catchment reset scope of `duck.db`**: confirm the keep/clear split table-by-table at build time
   against the live schema (new migrations may have added runtime tables — e.g. `alert_delivery` is an
   outbox: clear it).
5. **Quiesce vs reject** a mid-run Pond: repair quiesces (terminates + abandons the phantom); delete
   rejects (409). Recommend reset **quiesces** (it's a heavier, intentional operation) but gate the
   Catchment form on no active repair.

## Build order

1. `Driver.reset_pond` on the delete + refresh/repair primitives (clear registry/ledger/data, reset
   `pond_state`/`pond_target`, force cold rebuild, post-rebuild orphan prune) + the route/CLI/UI + tests.
2. `Driver.reset_catchment` (the filesystem sweep + `duck.db` runtime reset + reload + optional rebuild) +
   route/CLI/UI + tests.
3. (Later) undeploy, as its own plan.

## Tests

- Per-Pond: reset scrubs registry + data (incl. an orphan table and an Object the current code doesn't
  produce) + ledger; the Pond rebuilds its current tables; the orphan is gone; downstream coverage-misses
  and reloads; operational config (a window/spout/trigger) survives. Idle-gate/quiesce.
- Catchment: reset all → `duck.db` runtime cleared, artifacts + config + secrets intact; the chain rebuilds
  from Inlets down (real-Duck e2e, like the delete e2e); `--rebuild` drives it without a manual Tap.
- The package-upgrade shape: a data file in a "stale" format is cleared and rebuilt (a coarse proxy test).
