# Docs for 0.9.0: handoff

Written 2026-11-25 at the end of the session that built Records, Media and App scaling, for a fresh session to
write the docs from. Facts only: what exists, where it lives, which pages are out of date. Wording, structure and
examples are the next session's to decide (see the author's rule: docs written fresh, not carried over from a long
code session). Branch: `showcase-fixes` (95+ commits ahead of `main`, not pushed).

## Sources of truth

- `plans/records-and-media.md` (concepts, decisions, "As built") and `plans/app-scaling.md` (decisions, growth
  rule, settings, scaler interface, "As built", the GCP run). Both record the author's calls; follow them.
- `CHANGELOG.md` "Unreleased": the user-facing summary, already checked against the code.
- CLAUDE.md sections "Apps", "App scaling", "Records and Media".
- Code: `src/duckstring/app.py` (`app_config`, `_scaling`, `scale_settings`, the `duckstring.app` handle),
  `app_data.py` (Ponds reading Records and Media), `core.py` (`Pond.read_records`, `Pond.media`),
  `cli/app.py`, `cli/catchment.py` (`records`, `media`, their `move`), `cli/install.py`,
  `catchment/app_scaling.py` (`DefaultScaler`, `AppView`, `Decision`), `catchment/records.py`, `media.py`,
  `app_lifecycle.py`, `moves.py`, `egress/records.py`.
- A working App using Records in anger: the attention showcase's `wa_app/community.py` and Pond `wa_asked`
  (private repo `duckstring-cloud/showcase/attention/`). Patterns worth knowing, not copying verbatim: see below.

## Pages out of date or missing

Nothing in `docs/docs` mentions Records, Media, App scaling or `duckstring install` (searched for `records_pool`,
`read_records`, `DUCKSTRING_RECORDS_URL`, `app scale`, `min_instances`, `catchment records`, `duckstring install`:
no hits).

- `guides/building_apps.md`: "Current limits" says Apps can't use cloud compute or run several instances. Both are
  now possible. The page has no Records, Media, several-instances or major-handover material. Its "State" section
  predates Records (an App's data reaching a pipeline only through an API is superseded,
  `plans/records-and-media.md` header). Whether Records/Media/scaling go in this guide or in new guides under
  "Getting Data Out" (sidebar `docs/sidebars.ts` line ~62) is open.
- `reference/pond_toml.md` `[app]`: missing `records`, `media`, `migrate`, `instances`, `min_instances`,
  `concurrency`, `connections`, `stop_grace`, and the `[app.scale]` table; `[pond]` is missing `external`.
- `reference/cli/app.md`: has `instances`, `logs`, `restart`; missing `scale`, `drain`, `migrate`, `purge`.
- `reference/cli/catchment.md`: missing `catchment records` (`--use`, `--memory`, `--listen`, `--host`,
  `--tls-cert`, `--tls-key`, `--reader-url`), `catchment media` (`--use`), `catchment records move`,
  `catchment media move`.
- `duckstring install` has no CLI page, and `reference/cli/index.md` doesn't list it.
- `reference/python/app.md` (App Handle): missing `records_url()`, `records()`, `records_pool()`, `media_url()`,
  `media_path()`, `media()`, `report_load()`.
- `reference/python/pond.md`: missing `read_records(ref)` and `media(line, folder="")`.
- `reference/environment.md`: missing `DUCKSTRING_RECORDS_URL`, `DUCKSTRING_RECORDS_READER_URL`,
  `DUCKSTRING_RECORDS_TLS_CERT`/`_KEY`, `DUCKSTRING_MEDIA_URL`, `DUCKSTRING_MEDIA_ROOT`, `DUCKSTRING_PG_BIN`,
  `DUCKSTRING_PREVIOUS_MAJOR`/`_STATE`/`_MEDIA_URL` (given to `[app] migrate`), `DUCKSTRING_APP_INSTANCE`,
  `DUCKSTRING_APP_CONCURRENCY`, `DUCKSTRING_APP_CONNECTIONS`, `DUCKSTRING_APP_SCALER`,
  `DUCKSTRING_APP_REMOTE_START_TIMEOUT`, `DUCKSTRING_APP_ORPHAN_MINUTES`, and the older `DUCKSTRING_APP_STATE`,
  `DUCKSTRING_APP_DOWNLOAD`. Check each one's meaning in the code before listing it; some are set by the Catchment
  for the App rather than read from the operator.
- `reference/cli/spout.md` / `guides/delivering_with_spouts.md`: the `records:` destination.
- `reference/http_api.md` (route table): `/api/catchment/records|media` (+ `/move`, `/reader`), `/api/catchment/move`,
  `/api/app-data/...`, `/api/ponds/{name}/app/scale`, `.../app/instances/{id}/drain`, `.../app/migrate`.
- Concept pages: only `concepts/ponds.md` mentions Apps. Whether Records/Media earn a concept paragraph (the
  Catchment as catalog now holds App data too: catalog `records` in SQL) is open.
- Cloud guides (`cloud_compute_on_aws|azure|google_cloud.md`): an App's extra instances run on its declared compute;
  remote instances need `state = "none"`, Records reachable (`--listen`, TLS) and Media on an object store, else 503.
  Only the Google Cloud path has been run for real (below).
- `guides/upgrades.md`: a stateful App's major handover (trial, `pond promote` cutover, retired) is an upgrade path.

## Docstrings must match the reference

The public Python docstrings (`duckstring.app` functions, `Pond.read_records`, `Pond.media`, `MediaInput`) and the
CLI's command docstrings and `help=` strings are written to match `docs/docs/reference/`. Change both together.
The new ones were written in the build session and haven't been reconciled with any page.

## Behaviour facts a page will need

- `[app] records = true` needs a Pond name matching `RECORDS_NAME` (lower-case letters, digits, underscores,
  starting with a letter or underscore, at most 48 characters). The schema and role are `{name}_v{major}`; the
  role's `search_path` puts its own schema first. Every App line can read every other line; only its own is
  writable. Ponds and the viewer read as a separate reader role.
- `records()` needs psycopg in the App's environment; `records_pool()` needs `psycopg_pool` too. The pool's
  `max_size` defaults to `DUCKSTRING_APP_CONNECTIONS` = `[app] connections` (default: `concurrency`, default 4).
  The line role's `CONNECTION LIMIT` = instances x connections + 2.
- `instances > 1` requires `state = "none"` (refused at deploy otherwise). `min_instances <= instances`.
- `[app.scale]` keys: `requests` (0.7), `cpu` (0.7), `memory` (0.8), `load` (0.7) as shares in (0, 1]; `after`
  (10s), `socket_idle` (5s) as durations. Defaults in `app.SCALE_DEFAULTS`. The default rule grows by one when the
  time-weighted mean over `after` passes a threshold (changed during the GCP run; the plan's "growth rule" section
  and `DefaultScaler.decide` are current).
- Placement: no declared compute → all instances on the Catchment. Declared compute (`[pond] duck`) with
  `min_instances = 0` → a request with nothing ready starts on the Catchment; growth goes to the declared compute.
  `min_instances >= 1` → never on the Catchment. Scaling down drains the Catchment's instance first.
- `migrate` is allowed only with Records or Media. `pond promote` on a stateful App is the cutover (503 with
  `Retry-After: 5` during it); a trial line is Manage-only; a retired line redirects to the live one.
  `app purge NAME@MAJOR` refuses a deployed live line and asks for the Catchment's name. Data outlives `pond remove`.
- A Pond reading Records or Media: an Inlet always runs when asked; a non-Inlet needs `@ripple(always_run=True)`
  (or `always_run` on a SQL Ripple), since nothing in the engine sees an App's writes as a Source change.
- `[pond] external = ["app:feedback@1", "GDELT events"]` draws outside sources on the canvas only; `app:` ones open
  that line's data. It declares nothing to the engine.
- Local: `pond run` of a Pond reads Records snapshots at `puddles/records/{schema}/{table}.parquet` and Media at
  `puddles/media/`; `pond run` of an App gives it its own PostgreSQL under `puddles/app/records/` (needs
  PostgreSQL installed: `duckstring install postgres`).
- `duckstring install [postgres|extensions|EXTRA...] [--yes] [--dry-run] [--postgres-version 18]`: PostgreSQL from
  the PGDG apt/yum repositories or Homebrew; the image carries it. Binaries are found via `DUCKSTRING_PG_BIN`, the
  path `install` recorded, the usual install places, then the PATH.
- Moves: `catchment records move managed|postgres://... [--postgres-version N]`, `catchment media move
  local|s3://...|gs://...|az://...`. Apps with Records answer 503 during the Records copy; counts (Records) or
  listings (Media) are compared before the switch; the old store is deleted after it. A PostgreSQL major upgrade is a
  move to `managed` with `--postgres-version`.
- Backups: `catchment download` and the state backup carry Records as `records/records.dump`, and Media (the state
  backup only up to 256 MB).

## Patterns from the showcase (candidate recipe material)

From `wa_app/community.py`, a real App with several instances sharing Records:

- One pool per process, created lazily, DDL run under `pg_advisory_xact_lock` so instances starting together don't
  race on `CREATE TABLE IF NOT EXISTS`.
- Work done once across instances (naming a group with an LLM) claimed with `INSERT ... ON CONFLICT DO UPDATE ...
  WHERE claimed_at < now() - interval ... RETURNING`.
- Anonymous comments keyed by a browser-held random id, deletable with a token whose hash is stored.
- Records read back into the pipeline: Pond `wa_asked` (`always_run`, `external = ["app:wa_app@1"]`, the App's
  Records joined to `wa_trends`), with the App reading `wa_asked` as an optional Source. An optional Source gets
  demand only from its own Refresh; two floating Refreshes drift, so the showcase put both on one scheduled grid
  (`--every 15m --start 00:00`).
- DuckDB reserved words bite when Records columns are read through DuckDB: a column named `at` failed to parse in
  `read_records` SQL; `groups` is a window-frame keyword.

## What has run for real

- Records, Media, handover and moves: local tests against real PostgreSQL (`tests/test_records.py`,
  `tests/test_app_data.py`), including a real 17 → 18 version move.
- App scaling on Google Cloud (Compute Engine dedicated VMs, data root and Media on GCS, Records over TLS): see
  `plans/app-scaling.md` "The GCP run". Not run on AWS or Azure. Cloud Run instances reaching Records need VPC
  egress, not set up.
- The attention showcase on the author's laptop: Records with two instances' worth of traffic patterns, Gemini
  naming, `wa_asked` reading Records back.

Docs should say what's supported without "yet"; where something hasn't been run on a cloud, say so plainly or leave
it out (the author's call when writing).

## Media for the pages

Showcase screenshots (desktop and phone, the comments panel, "Searched here lately", "What people search for here",
named search groups) were taken in the build session at
`/private/tmp/claude-501/-Users-isaac-harvey-code-isaac-harvey-public-duckstring/2b3566b0-0904-47e4-b328-81ca7467f400/scratchpad/shots/`
(temporary; they show the showcase, not a docs example). The web UI's App data section (`AppDataTree`,
`AppDataViewer`, `MediaPanel`) and the App node's table icon have no captures yet. Docs media conventions are in
CLAUDE.md "Writing docs".

## After the docs

Release steps, in order, each needing the author's go-ahead where marked: Trickle 0.4.0 tag and PyPI publish
(go-ahead; only 0.3.1 is on PyPI and Duckstring needs `>=0.4.0`); bump `pyproject.toml` to 0.9.0 and date the
changelog section; push `showcase-fixes` and open the PR (go-ahead); RC and the AWS smoke test
(`reference_aws_smoke_test` memory; go-ahead for AWS spend). Open decision for the author: whether 0.9.0 ships with
App scaling run only on Google Cloud, or after AWS and Azure runs.
