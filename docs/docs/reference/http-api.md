---
title: HTTP API
description: The Catchment's REST surface.
---

# HTTP API Reference

Everything the CLI and [web UI](../guides/web-ui.md) do goes through this API, served by the Catchment under `/api`. All timestamps are UTC ISO-8601 strings; all bodies are JSON unless noted.

When the Catchment is started with an API key (`duckstring catchment init --key …` or the `DUCKSTRING_API_KEY` environment variable), every `/api` request except `/api/health` must carry it — `Authorization: Bearer {key}` — and is `401` otherwise. The CLI sends the key registered against the Catchment (`catchment connect --key …`) automatically.

## Health

```
GET /api/health
```

Returns `{"status": "ok"}` when the Catchment and its database are reachable.

## Deploy

```
POST /api/deploy
```

Two forms, distinguished by content type:

- **Upload** (`multipart/form-data`): fields `name`, `version`, `type`, and `pond` — a zip of the project. This is what `duckstring pond deploy` sends.
- **Git** (`application/json`): `{"name", "version", "type", "git_ref", "repo_url"}` — the Catchment clones `repo_url` and checks out `git_ref`.

Registers the version, selects it for its major, validates the graph (`422` on inter-Pond cycles or a bad archive), and auto-clears any failure on the Pond. See [Deploying](../guides/deploying.md).

```
GET /api/ponds/{name}/versions/{version}
```

Whether that exact version exists, and whether it's the selected one: `{"name", "version", "is_active"}`. `404` if never deployed.

## Status

```
GET /api/status
```

The full live state, as one document — this is what the UI and `duckstring status` poll:

```json
{
  "ponds": [
    {
      "id": "sales@1", "name": "sales", "major": 1, "kind": "pond", "version": "1.0.0",
      "status": "running",
      "gen": 12, "runs_completed": 11,
      "has_pull": true, "target_f": null,
      "start_f": "2026-06-11T09:30:00+00:00", "end_f": "2026-06-11T09:29:57+00:00",
      "d_ms": 0, "trigger": null,
      "is_failed": false, "is_blocked": false, "is_killed": false,
      "failed_f": null, "failures": 0,
      "immediate_retries": 1, "source_retries": 2,
      "ripples": [
        {"name": "join_lines", "status": "running", "gen": 12, "runs_completed": 11,
         "has_pull": true, "target_f": null, "start_f": "…", "end_f": "…"}
      ],
      "ripple_edges": [["daily_sales", "join_lines"], ["price_tiers", "join_lines"]]
    }
  ],
  "edges": [["transactions@1", "sales@1"], ["products@1", "sales@1"], ["sales@1", "reports@1"]]
}
```

Field notes:

- `id` — the pond key `name@major`: one entry per deployed **major line** ([concurrent majors](../concepts/versioning.md) appear as separate live Ponds). `edges` reference these ids.
- `status` — one of `failed | killed | blocked | running | queued | idle`, in that precedence (a failed Pond reads `failed` even if work is queued behind it).
- `start_f` / `end_f` — the node's [freshness](../concepts/freshness.md) at run start / completion; `null` means never-run. `target_f` is the nearest unsatisfied push target; `has_pull` is the pull token.
- `gen` / `runs_completed` — runs started / completed since the Catchment loaded.
- `d_ms` — the Pond's window-derived freshness duration (0 without [Windows](../guides/windows.md)).
- `trigger` — the standing trigger, e.g. `{"kind": "tide", "bound_ms": 14400000}`, or `null`.
- The fault fields (`is_failed`, `is_blocked`, `is_killed`, `failed_f`, `failures`) and live budgets are described in [Fault Tolerance](../guides/fault-tolerance.md).
- `edges` — the inter-Pond graph as `[source, sink]` pairs; `ripple_edges` the intra-Pond graph as `[parent, child]`.

## Run history

```
GET /api/runs?pond={name}&major={int}&version={semver}&lineage=true&ripples=false&limit=100
```

Recent Pond Runs, newest first. `pond` filters to one Pond — and with `lineage=true` (the default) its upstream Sources too; `major`/`version` narrow to one major line (default: the highest deployed); `ripples=true` nests each run's Ripple Runs; `limit` clamps to [1, 1000].

```json
{
  "runs": [
    {
      "pond": "sales", "id": "sales@1", "major": 1, "version": "1.0.0", "f": "2026-06-11T09:30:00+00:00",
      "started_at": "…", "finished_at": "…",
      "status": "success", "error": null, "traceback": null,
      "ripples": [
        {"ripple": "daily_sales", "retry": 0, "status": "success",
         "started_at": "…", "finished_at": "…", "error": null, "traceback": null}
      ]
    }
  ]
}
```

Run `status` is `running | success | failed | killed`. Ripple Runs carry one record **per attempt** — `retry` is the attempt index, so a Ripple that needed its [immediate-retry budget](../guides/fault-tolerance.md) shows multiple rows. Failures carry `error` and, for exceptions, the full `traceback`.

## Triggers & control

All under `/api/ponds/{name}/…`, all returning `{"ok": true}`; `404` for unknown Ponds, `422` for invalid payloads. Every route takes optional `major` / `version` query params selecting the major line to act on: `major` picks the line (default: the highest deployed), `version` additionally requires that exact version to be the line's selected artifact (`422` if it isn't). Semantics in [Triggers](../guides/triggers.md) and [Control](../guides/control.md).

| Endpoint | Body | Action |
|---|---|---|
| `POST …/tap` | — | Pull once |
| `POST …/pulse` | — | Push once to now |
| `POST …/wave` | — | Standing pull |
| `POST …/tide` | `{"bound_seconds": 14400}` | Standing push with staleness bound |
| `POST …/untrigger` | — | Remove the standing trigger |
| `POST …/wake` | — | One-shot non-propagating pull |
| `POST …/force` | — | Recompute at current freshness |
| `POST …/sleep` | `{"upstream": false}` | Clear demand (optionally ancestors too) |
| `POST …/kill` | — | Terminate the worker; park `killed` |
| `POST …/clear` | — | Reset failed/killed; unblock downstream |
| `GET …/budget` | — | `{"immediate_retries", "source_retries"}` |
| `POST …/budget` | `{"immediate_retries": 1, "source_retries": 2}` | Set the live retry budgets |

## Windows

| Endpoint | Description |
|---|---|
| `GET /api/ponds/{name}/windows` | `{"windows": [...]}` |
| `POST /api/ponds/{name}/windows` | Add a rule: `{"name", "start_anchor", "duration_seconds", "freq_unit", "freq_interval", "valid_days", "until_time"}`. `freq_unit` ∈ `SECOND \| MINUTE \| HOUR \| DAY \| WEEK`; `valid_days` like `"MON,WED,FRI"` or `null`; `422` on overlap. |
| `POST /api/ponds/{name}/windows/{window}/remove` | Remove a rule (`404` if absent). |

## Data

See [Querying Data](../guides/querying-data.md). Reads always hit the exported Parquet snapshots, never live state.

```
POST /api/query
```

`{"pond", "major"?, "version"?, "ripple"?, "sql"?, "format"?}` — runs `sql` against the Pond's exported tables (default: `SELECT * … LIMIT 10` on `ripple`; `major`/`version` select the major line to read, default the highest). Without `format`, returns JSON rows; with `"csv" | "json" | "parquet"`, returns the file. `400` on SQL errors.

```
GET /api/ponds/{pond}/ripples/{ripple}?major={int}
```

The Ripple's published output, zipped. `404` if it has no export yet.

## Worker protocol (informational)

Two further endpoints exist for the Catchment's own worker processes (Ducks) — listed for completeness, not for external use: workers hold a short poll on `GET /api/duck/{name}/{major}/jobs` for commands (`begin_run` / `shutdown`) and report progress to `POST /api/duck/{name}/{major}/events`, idempotently on freshness (one Duck per major line). Both are worker-initiated — the dial-back design that lets local and remote workers share one protocol. See [Architecture](architecture.md).
