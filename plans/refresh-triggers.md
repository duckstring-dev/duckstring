# One trigger: Refresh

Status: **planned, not built.** Settled with the author on 2026-10-13 and 2026-10-14, after the Pulse
throttle (`plans/pulse-throttle.md`, merged in #34) made a fast Pulse pace itself to the bottleneck. Taps
(on-request freshness) are agreed in outline and get their own plan and stage.

## Why

Scheduling from the end of the pipeline is already the shift we ask people to make. Four trigger names
(Tap, Wave, Pulse, Tide) on top of it is too much to learn, and the two pull triggers are the hardest to
explain ("whatever is next", "one stage ahead of each consumer"). With the throttle, a short Tide runs as
often as a Wave and usually delivers fresher data, so the user-facing model can be one request plus a
standing version of it.

The names also describe the pipeline. "Refresh" describes what happens to the *data*, which is what the
user cares about: you refresh `reports`, and Duckstring works out what runs.

## Settled decisions

- **One trigger, Refresh.** It asks for data at least as fresh as now (today's Pulse). Tap, Wave, Pulse and
  Tide leave every user surface (CLI, HTTP API, web UI, docs, Playground).
- **A standing Refresh repeats it**, with the same recurrence vocabulary windows use:
  - `refresh add POND --every D`, **floating**: due `D` after the previous request, so any refresh (a manual one, an
    anchored one) resets its phase. This is today's Tide clock. The bound is a request period, so data
    can be up to `D` plus the pipeline's run time old; "max age" was rejected as a name for that reason.
  - `--every D --start HH:MM`, **anchored**: fires on its grid regardless of other refreshes.
  - `--on DAYS` and `--until T` filter and end the recurrence, as on a window.
  - `--continuous`, also accepted as `--every 0s`: as fresh as the pipeline allows, paced by the throttle.
    It replaces the Wave.
- **Several per Pond, like windows.** Any number of anchored refreshes; at most one floating refresh
  (continuous included), and adding another **replaces it silently**, as setting a Wave or Tide does
  today. Several floating refreshes would add nothing, since any refresh resets the floating phase.
- **"On request" means any time the data is queried**, and it is configured in Duckstring by the data
  engineer, not left to each app to send a refresh with its queries. It evolves from the duct's
  `--tap-on-get`. Inspection inside Duckstring (the web UI, the CLI's data commands) never triggers runs.
  The design is outlined in "On request: Taps", a separate stage.
- **Pull leaves the engine**: see "Removing pull".
- **Positioning keeps "pull orchestration"** (demand starts at the end of the pipeline). "Demand-driven"
  suggests learned load balancing more than a different kind of orchestrator. Inside the engine there is
  only demand.
- **Windows are the mirror image**: a window says when data may be requested from an Inlet, a refresh
  says when it is requested of an Outlet. A business-hours shape ("every 30 min from 08:00 for 10 h,
  weekdays") comes later, on both.
- **The existing control Refresh is renamed Rebuild** (it is a cold wipe-and-rebuild), freeing the word.
  Repair stays Force plus Rebuild.
- **CLI: every group is `group verb POND`**, as `spout` and `alert` already are: `refresh add|ls|rm POND`
  and `window add|ls|rm POND`, both top-level, with the bare `refresh POND` as the one-shot. The `trigger`
  group goes.
- **Internal names follow the product**: Trigger, Pulse and Tide become Refresh in code too, and push
  becomes **demand** (a Pond's `targets` are its demanded freshnesses). See "Internal names".

## The model

| Need | CLI | Engine |
|---|---|---|
| Refresh now, or on request | `duckstring refresh reports` | `pond_add_target(now, from_trigger=True)` |
| Refresh periodically | `refresh add reports --every 1d` | floating: today's Tide rule, bound = `every` |
| Refresh at set times | `refresh add reports --every 1d --start 06:00 [--on MON,...] [--until T]` | anchored: the latest occurrence `c <= now` as a target |
| As fresh as possible | `refresh add reports --continuous` (= `--every 0s`) | floating with bound 0 |

**Anchored refreshes request the occurrence time, not `now`.** Each tick, an anchored refresh adds target
`c`, the latest kept occurrence at or before `now` (`start + k * every`, filtered by days and `until`).
`pond_add_target` already ignores `c <= end_f`, `c <= start_f` (rule 6) and duplicates, so this needs no
"last fired" state, and a Catchment that was down at 06:00 catches up with one request when it returns.
The trade-off: the Inlet stamps the minted epoch `c`, so after downtime the data is labelled 06:00 even if
read at 09:00. That is conservative (never fresher than labelled) and matches the minted-freshness rule.

**Floating refreshes keep today's Tide clock**: fire when `now + D - (max(targets) ?? start_f) >= every`.
Anchored and manual refreshes add targets too, so they reset the floating phase, as decided.

`next_wake` covers both: an anchored refresh's next occurrence, and the floating deadline as today.

## Engine (`engine/core.py`, `engine/catchment.py`)

- `Refresh` dataclass: `pond_id`, `name`, `every: timedelta`, `start: datetime | None`, `valid_days`,
  `until`. Anchored occurrence math reuses `Window`'s grid (`_delta`, `_kept`, `next_open`); factor it
  into a shared recurrence helper instead of duplicating it. `duration` has no meaning for a refresh.
- `EngineState.refreshes: dict[PondId, list[Refresh]]` (at most one floating per Pond, enforced by the
  Driver). `tick` replaces the Tide branch with the floating rule and adds the anchored rule; both call
  `pond_add_target(..., from_trigger=True)`, so the throttle sees them as trigger demand.
- `refresh_pond` (today the Rebuild flag) is renamed `rebuild_pond`, `PondState.refresh_pending` becomes
  `rebuild_pending`, and `BeginRun.refresh` becomes `rebuild` (the Duck job field too; a Duck must match
  its Catchment's version, so no wire compatibility is needed). The Duck executor and dbt executor follow.
- `pulse_pond` becomes `refresh_now`; see "Internal names" for the rest. Pull goes with "Removing pull".

## Removing pull

With Tap and Wave gone, nothing a user sets creates pull. What remains uses it only as a local "run when a
Source is fresher" signal, and each case is a demand target that isn't forwarded:

| Today (pull) | Becomes |
|---|---|
| Wake: a one-shot, non-propagating pull | a local demand at the current `sourceF` (if `> end_f`), not forwarded; still clears a failure |
| A Spout's standing Wake | each tick, a local demand at its Source's visible freshness when it is newer than its own `start_f` (per window, as now) |
| Missing-asset retry (`block_on_missing_asset`) | a local demand gated by `missing_asset_f`, as the pull was |
| Tap-on-get | the on-request refresh (below) |
| Duct pull forwarding (poller to upstream `/tap`) | gone; a Draw only ever carries demand |
| `restore_demand` | gone; demand is restored by reload's `resend_held_demand`, rule 8 and the standing refreshes |

That removes `has_pull`, `has_received_pull`, `pull_m`, `pull_local`, the Ripples' `has_pull`,
`pond_receive_pull`, `pond_set_has_pull`, `ripple_set_has_pull`, `tap_pond`, `restore_demand`, the Wave
branch and re-tap, and the `startF` cold-start guards (one of the subtle rules in CLAUDE.md). Migration
`031` drops the `pond_state` pull columns. One demand mechanism is easier to reason about, document and
test, and the theory doc's model shrinks to demand plus the throttle (the Kanban idea survives as the
motivation for the bottleneck's release at its starts).

"Measure first" found no need for lookahead: a continuous refresh delivers fresher data than a Wave even
where noise costs it Outlet runs.

## Internal names

Code and docs stay findable from each other:

- `Trigger`/`EngineState.triggers` become `Refresh`/`refreshes`; `pulse_pond` becomes `refresh_now`;
  `from_trigger` becomes `from_refresh`; Tide wording in comments becomes floating refresh.
- Push becomes **demand**: `PondState.targets`/`RippleState.targets` become `demand`, `pond_add_target`
  becomes `add_demand`, `ripple_add_target` becomes `ripple_add_demand`, `push_sources` becomes
  `demand_sources`, `min_target`/`max_target` become `min_demand`/`max_demand`. The `pond_target` table
  becomes `pond_demand` in `031`.
- The control Refresh becomes Rebuild throughout (above).
- The throttle's names (`ThrottleState`, slow, held, release) stay.
- CLAUDE.md's "Terminology for user-facing text" keeps "pull" as positioning language only (demand starts
  at the end of the pipeline, as the landing page's pillar says, and the landing page keeps it); inside
  the engine there is only demand.

## Persistence (migration `031`)

- New `pond_refresh` (PK `(pond_id, name)`): `every_ms`, `start_anchor` (NULL = floating), `valid_days`,
  `until_time`, `created_at`. Operational config, surviving redeploys, like `pond_window`.
- Convert `pond_trigger`: `tide` becomes a floating refresh with `every_ms = bound_ms`; `wave` becomes a
  continuous one (`every_ms = 0`). Names default as below. Then drop `pond_trigger`.
- Rename `pond_state.refresh_pending` to `rebuild_pending` (`ALTER TABLE ... RENAME COLUMN`).
- `Driver.reload` loads `pond_refresh` into `EngineState.refreshes`. The reload re-send
  (`resend_held_demand`) is unchanged.

Default names, as the window editor suggests them: "Every 30 min", "Daily", "Daily at 06:00", "Weekdays
at 06:00", "Continuous".

## Driver and HTTP API

- `POST /api/ponds/{name}/refresh` (demand): with no body, a one-shot refresh (optional `at` for a duct's
  epoch, as `/pulse` takes today). With `{every_seconds, start?, days?, until?, continuous?, name?}`, add a
  standing refresh (a floating one replaces the existing floating one). `every_seconds = 0` is allowed.
- `GET /api/ponds/{name}/refreshes` (read) and `DELETE /api/ponds/{name}/refreshes/{refresh}` (demand).
- `POST /api/ponds/{name}/rebuild` (full) takes over today's `/refresh` (the Rebuild flag), with `clear`.
- `/api/status`: per-Pond `trigger` becomes `refreshes: [{name, every_ms, start, days, until, next_due}]`;
  `next_due` from an engine helper shared with `next_wake`.
- `POST /api/ponds/batch` and `duckstring do`: the operation `refresh` becomes `rebuild` (precedence order
  `kill > sleep > reset > wipe > remove > clear > repair > rebuild`).
- `auth.py`: the demand level's description (tap/wave/pulse/tide) becomes refresh.

**Compatibility.** `/refresh` changes meaning, from Rebuild (full) to the trigger (demand). Two Catchments of
different versions meet across a duct: a new downstream calling `/refresh` on an old upstream would, with a
full key, flag a Rebuild there. So for one release:

- the poller keeps calling `/pulse` (and `/tap` for pull, if any remains), and
- `/tap`, `/wave`, `/pulse`, `/tide` and `/untrigger` stay as deprecated aliases (`pulse` and `tap` send a
  one-shot refresh, `tide` sets a floating refresh, `wave` a continuous one, `untrigger` removes the
  floating one).

The release after, the poller moves to `/refresh` and the aliases go. The changelog flags the `/refresh`
change in bold for anyone scripting the old Rebuild route.

## CLI

Every group follows the existing `group verb POND [NAME]` shape (`spout add POND`, `spout rm POND NAME`):

```
duckstring refresh POND                              # refresh now (opens the live status view, as pulse does)
duckstring refresh add POND --every 30m              # floating; replaces the floating one silently
duckstring refresh add POND --every 1d --start 06:00 --on MON,TUE,WED,THU,FRI --name morning
duckstring refresh add POND --continuous             # = --every 0s
duckstring refresh ls POND
duckstring refresh rm POND NAME
duckstring window add POND --every 1d --start 02:00 --duration 1h [--on ...] [--until ...] [--name ...]
duckstring window ls POND
duckstring window rm POND NAME
duckstring control rebuild POND [--clear]            # today's `control refresh`
```

`refresh` is a group whose bare form takes a Pond: a custom Click group resolves `add`, `ls` and `rm` as
subcommands and anything else as the Pond to refresh now (a Pond named `add`, `ls` or `rm` is refreshed with
`refresh -- add`). The `trigger` group goes; for one release `duckstring trigger tap|wave|pulse|tide|remove|
window` remain as hidden aliases that print the new command. `duckstring pond deploy`'s next-step hint (`cli/pond.py`) and the
missing-data hint in `routes/data.py` (`trigger pulse`) change to `refresh`. CLI docstrings and `help=`
strings match `docs/docs/reference/cli/`, so change both together.

`duckstring duct open --tap-on-get` is superseded by on-request refresh (below); keep it as a hidden alias
for one release.

## On request: Taps (separate stage)

A query is a demand signal: if an app is being used, its data should stay fresh, and when it stops, the
pipeline should go quiet. That is pull orchestration carried to the consumer's edge, and a feature no
scheduler offers. It belongs to the data engineer, so it is configured in Duckstring, not by each app
sending refreshes with its queries.

The policy belongs to the **access point**, not the Pond: one Pond can serve an app that wants freshness and
a notebook that is happy with whatever is there, and inspection inside Duckstring must never trigger runs.
So the proposal is a named access point on a Pond, a **Tap** (the name freed by the old trigger; Spouts push
data out, Taps let apps draw it):

- **Scope**: a Pond (one major line), optionally limited to some tables.
- **Credential**: its own key. Queries are classified by the credential they arrive with, so the web UI,
  the CLI and any other key never match a Tap and never refresh. The key reads the Tap's scope and implies
  only the Tap's refresh, so an app can't trigger arbitrary refreshes.
- **Policy**: `--fresh-within D`, **default 0** (freshness is the reason to create a Tap). It uses the
  floating refresh's clock, evaluated on a query instead of on a tick: a query sends a one-shot refresh
  when the most recent demand on the Pond (or, with none pending, its freshness) is older than `D`, i.e.
  `now + D_window - (max(demand) ?? start_f) >= D`. So `--fresh-within 1d` behaves like `--every 1d` while
  the app is in use, and does nothing when it isn't. The query is served immediately from the current
  snapshot, as tap-on-get did; `--wait` blocks until the refresh lands, with a timeout. Bursts of queries
  are paced by the throttle.
- **Surface**: `duckstring tap add|ls|rm POND` (as `spout`), the query route taking the Tap's key (and later
  the Flight SQL and Postgres wire adapters, by credential), a Taps section in the web UI next to Spouts,
  and query counts per Tap in `/metrics`.

Building on `pond_open`/tap-on-get and the serving core (`catchment/serving.py`). Agreed in outline
(2026-10-14); it gets its own plan (`plans/taps.md`) and ships after the trigger change.

## Web UI (`frontend/`)

The Sidebar's Triggers row (Tap/Wave/Pulse/Tide buttons plus the Tide input) becomes a **Refresh** section,
built on `WindowEditor`'s pattern:

```
REFRESH                                   [ Refresh now ]
  Daily at 06:00 (Mon-Fri)                 next 06:00 Tue     x
  Every 30 min                             next in 12m        x
  > Add a refresh

  Every   [ 30 ] [ min           v ]   <- the unit list ends with "continuously", which hides the number
  Name    [ Every 30 min           ]   <- suggested from the fields, editable
  > Options
      Start   [ 06:00 ]    empty = counts from the previous refresh
      Days    [Mon][Tue][Wed][Thu][Fri][Sat][Sun]
      Until   [ date time ]
  [ + Add Refresh ]
```

- Share the recurrence fields with `WindowEditor` as one component (a refresh has no Duration).
- The "next" column comes from `/api/status` `next_due`. It makes the floating behaviour visible: "next
  in 12m" jumps when someone clicks Refresh now.
- The canvas `TriggerNode`: one node per Pond, labelled with its most frequent refresh and a count badge
  ("Daily 06:00 · +1"), one colour (no pull/push pair). `DemandIndicators` and the `THEME_PULL`/`THEME_PUSH`
  pair in `store.ts` simplify to one demand colour.
- Access: Refresh stays at demand level (as Triggers are); windows stay full-only. While a Pond is failed or
  blocked the section is replaced by the reason box, as now.
- The Selector's Refresh operation is renamed Rebuild. `api.ts` (`refreshPond` becomes `rebuildPond`; new
  `refreshNow`, `addRefresh`, `removeRefresh`), `types.ts` (`refreshes` on a Pond) and `store.ts` follow.
- Read `frontend/AGENTS.md` first (Next 16).

## Playground (`playground/`)

The Playground is the first place people meet the orchestration, so it gets the new model first and gets
smaller:

- **Sidebar**: the Triggers row and its Tide input become the Refresh section above (Refresh now, every,
  start, continuous). The **Control panel goes** (Kill, Wake, Force, Sleep): failure handling isn't the
  point of a toy, and removing a refresh stops a pipeline. Windows, durations, variability and adding
  Ponds stay.
- **Engine** (`orchestration.ts`): add anchored refreshes and multiple refreshes per Pond, mirroring the
  Python engine, so the cross-check (`tests/throttle_crosscheck.mts`) can cover them. Pull goes with
  "Removing pull" (the spec's Wave yardstick becomes recorded numbers), and the internal renames apply.
  Kill, Sleep and Force stay in the engine for parity (their UI goes).
- **Types and store** (`types.ts`, `store.ts`): `ActiveTrigger` becomes a list of refreshes per Pond;
  actions `triggerTap`, `triggerWave`, `triggerPulse`, `triggerTide`, `setTideDraft` and the control
  actions give way to `refreshNow`, `addRefresh`, `removeRefresh`.
- **Canvas** (`TriggerNode.tsx`, `PondNode.tsx`, `DagCanvas.tsx`, `DemandIndicators.tsx`): as the web UI.
- **Console** (`ConsolePanel.tsx`) and the engine's log messages: "Pulse on", "Tap on", "Sleep on" and
  similar become refresh wording; throttle log lines stay (the playground is where they are useful).
- **Tour** (`src/lib/tourSteps.ts`, `tests/e2e/tour.spec.ts`): rewrite around the new model. Today's steps
  `wave`, `steady`, `run-chart`, `noise`, `adapts` teach pacing through a Wave, and `tide`/`tide-explained`
  a Tide. A sketch: the pipeline, the slowest step, running from the end, **Refresh now** (one pass,
  everything at one freshness), **Refresh continuously** (paced to the slowest step: the throttle chapter
  that was D5 of the throttle plan), the run chart, noise, **Refresh every N s**, a second output, done.
  The `noise`/`adapts` claim holds on the demo under a continuous refresh (3% fewer Outlet runs at sigma
  0.5, with fresher data; `bench/continuous_refresh/`).
- **Tests**: `tests/unit/orchestration.spec.ts` and `throttle.spec.ts` swap the Wave yardstick for recorded
  numbers; the tour e2e changes with the steps (`npm test` runs it at four viewports).

## Docs to rewrite

Every page below names Tap, Wave, Pulse or Tide (counts from a grep on 2026-10-13), or the Rebuild-to-be.
The concept and guide pages need rewriting, not search and replace: much of their teaching (pacing, the
"one run ahead" rule, the patterns table) is framed around the four triggers.

| Page | What changes |
|---|---|
| `concepts/orchestration.md` (19) | The Triggers section and table become Refresh and its standing forms. "Throttling to the bottleneck" teaches pacing through the throttle under a continuous refresh, not the Wave's one-run-ahead rule (keep the gantt idea). Standard Patterns table. The video caption ("kept current by a Wave"), and the video itself (`/img/ripple.webm`, `.mp4`), re-recorded under a continuous refresh. The optional-Source paragraph. |
| `guides/scheduling.md` (21) | Restructure around `refresh`: now, `--every`, `--start` (new: refresh at set times), `--continuous`, several refreshes per Pond, windows (`duckstring window`), different cadences, optional Sources, stopping (`--remove`). |
| `reference/orchestration_theory.md` (45) | **Removed** (the author is turning it into a blog post). Its links go: the sidebar (`docs/sidebars.ts`), the concept page's "See also", and CLAUDE.md (below). Precise rules a reader needs (the forms of Refresh, that `--every` is a request period, the window delay) move to the Scheduling guide and the `refresh`/`window` CLI reference; the formal model lives in the code and the playground engine. If the post is written from it, it describes the new model: demand plus the throttle, with Kanban as the motivation for the bottleneck's release at its starts. |
| `reference/cli/trigger.md` (13) | Replaced by `reference/cli/refresh.md` and `reference/cli/window.md`; `cli/index.md` (2) and the sidebar (`docs/sidebars.ts`) follow. |
| `reference/cli/control.md` | `refresh` becomes `rebuild`. |
| `reference/cli/do.md` | The `refresh` operation becomes `rebuild`. |
| `reference/http_api.md` (5) | Route table: `/refresh` (new meaning), `/refreshes`, `/rebuild`, the deprecated aliases. |
| `quickstart.md` (10) | "Run it once with a Pulse" becomes `duckstring refresh reports`; the trigger table becomes the refresh forms. |
| `guides/connecting_catchments.md` (4) | Duct demand, `--refresh-on-get`. |
| `guides/monitoring_and_failures.md` | Rebuild. |
| `guides/querying.md` | Tap-on-get becomes Taps (on-request freshness); any Rebuild mention. A Taps guide (or a section here) when Taps ship. |
| `guides/migrating_a_project.md` (3), `guides/upgrades.md`, `guides/dbt_projects.md`, `guides/cloud_compute_on_aws.md`, `concepts/management_and_execution.md`, `reference/pond_toml.md`, `reference/formats.md`, `reference/cli/catchment.md` (1 to 2 each) | Single mentions: reword. |
| `README.md` (2) | `trigger tide reports 1d` and `trigger pulse reports` become `refresh`. |
| `CHANGELOG.md` | An Unreleased entry: the new trigger, the removed ones, Rebuild, the `/refresh` route change in bold, and the aliases' end-of-life. |
| Landing page (`docs/src/components/Landing/index.tsx`) | No trigger names today; check the pull-orchestration pillar's wording still reads right ("schedules are set at the end of the pipeline"). |
| Blog drafts (private `duckstring-blog`) | Any draft naming the four triggers. |

`docs/documentation_style.md` applies (no em-dashes; check every claim against the code). CLAUDE.md's
"Terminology for user-facing text" section (Pull and push, Tide leading with "every x") is rewritten for
Refresh: lead with "every", and say that `--every` is a request period, so data can be up to the period
plus the pipeline's run time old. Its structure map, the orchestration model (the subtle rules lose the
cold-start guards) and the Triggers and control section follow the renames. With Orchestration Theory gone,
its opening paragraph names the code as the source of truth and `playground/src/lib/orchestration.ts` as the
simulation it is ported from and cross-checked against; the "Writing docs" list of Reference pages and the
Playground-linking list ("Link it wherever orchestration is explained") drop Orchestration Theory.

## Tests

- **Engine** (`tests/test_engine.py`): anchored refreshes (occurrence targets, days, `until`, catch-up after
  a gap is one request, idempotent with rule 6); floating refresh = the Tide tests renamed; a manual
  refresh resets the floating phase but not an anchored one; several anchored plus one floating per Pond;
  `next_wake` with anchored refreshes. Keep the Wave-yardstick throttle tests.
- **Driver/API** (`test_restart`, `test_catchment_app`, `test_status_api`, auth audit): `pond_refresh` CRUD
  and reload; migration `031` from Waves and Tides; floating replaces floating; `/rebuild`; the aliases.
- **CLI**: `refresh` forms, `window`, `control rebuild`, `do` with `rebuild`, the hidden `trigger` aliases.
- **Cross-check**: extend `tests/throttle_crosscheck.mts` with anchored refreshes once the playground has them.
- Gates as ever: `pytest`, `ruff check .`, playground `npm test`, frontend tests.

## Measure first

Measured 2026-10-14 (`bench/continuous_refresh/`): a continuous refresh against a Wave on the 11 throttle
topologies, with lognormal run-time noise (sigma 0 to 1), 8 paired seeds, in both engines.

- Without noise it matches the Wave's Outlet runs everywhere, with data at least as fresh (two to three
  times fresher on long pipelines).
- With noise it completes fewer Outlet runs on long pipelines (long lead: 16% fewer at sigma 0.5, 26% at
  sigma 1), but its data is still fresher: mean age no higher in 42 of 44 cases and about half the Wave's
  on long pipelines, p95 too. For "as fresh as possible", age is the measure, so **pull can go without a
  lookahead rule**.
- Two refinements for later, neither blocking. A slow Inlet joined with a fast sibling Inlet runs 7 to 13%
  staler under heavy noise: a release reaches both at once, the fast one reads while the slow one is still
  busy, and the join takes the older stamp (under a Wave both are asked when the join starts, so they read
  together). Being an Inlet isn't the cause; a slow Inlet alone matches the Wave. A possible fix: a Pond
  with a throttled required Source sends a release to its other Sources only when the throttled one starts
  the run that serves it. And upstream compute rises by a median of 1 to 4% (at most 24%) because the slow
  flag clears whenever the bottleneck idles and the pipeline refills. Both are throttle tuning, not reasons
  to keep pull.

## Ship order

1. Measure: done (see "Measure first").
2. **Engine**: `Refresh`, anchored and floating rules, `next_wake`, the Rebuild rename, pull removed and
   push renamed demand; tests (recording the Wave yardstick numbers first).
3. **Driver, migration `031`, HTTP API**, aliases; tests.
4. **CLI**: `refresh`, `window`, `control rebuild`, `do`, aliases; CLI reference pages together.
5. **Web UI**: the Refresh section, trigger node, Selector rename.
6. **Playground**: engine parity, UI cut-down, tour rewrite.
7. **Docs** (the table above), CLAUDE.md, the changelog.
8. A later release: remove the aliases and move the poller to `/refresh`.
9. **Taps**, from their own plan.

## Open questions

None outstanding for this stage. Taps' open questions belong to their own plan.
