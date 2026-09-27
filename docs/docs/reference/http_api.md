---
title: HTTP API
description: Authentication, conventions and routes.
---

# HTTP API

Everything the CLI and web UI do goes through the Catchment's HTTP API. This page covers authentication, shared conventions and the available routes. For request and response bodies, every Catchment serves its own schema:

| Path | Contents |
|---|---|
| `/openapi.json` | The OpenAPI schema. |
| `/docs` | Interactive documentation (Swagger UI). |
| `/redoc` | Reference documentation (ReDoc). |

All routes below are under `/api`, apart from `/metrics`.

## Authentication

A Catchment is either open, or protected by API keys. An open Catchment treats every request as having full access. This is also the right setting when a hosting platform authenticates requests before they reach the Catchment.

With keys configured, send one as a bearer token:

```bash
curl -H "Authorization: Bearer $DUCKSTRING_KEY" http://127.0.0.1:7474/api/status
```

Keys come in three levels, each including the ones before it:

| Level | Allows |
|---|---|
| `read` | Status, run history, lineage, data and catalog queries. |
| `demand` | Also triggers: tap, wave, pulse, tide and removing a trigger. |
| `full` | Everything else: deploying, control actions, windows, Spouts, secrets, alerts, ducts, compute and Catchment settings. |

A missing or unrecognised key gets `401`; a key below the route's level gets `403`. Tracebacks in run history are only returned to `full` keys; lower levels get the error message alone.

## Conventions

**Targeting a major line.** Routes that act on one Pond take the Pond name in the path and two optional query parameters:

| Parameter | Description |
|---|---|
| `major` | The major line. Defaults to the highest deployed. |
| `version` | A full version such as `1.2.0`. Must be the major line's currently deployed version, otherwise `422`. |

**Status codes.**

| Code | Meaning |
|---|---|
| `401`, `403` | Authentication, as above. |
| `404` | Unknown Pond, table, Object, Spout, window or channel. |
| `409` | The operation needs the Pond idle, or conflicts with its current state. |
| `422` | Invalid input, including a deployment that breaks a version pin, and a failed confirmation for irreversible operations. |

A connection test (`/spouts/test`, `/alerts/{name}/test`) that fails returns `200` with `{"ok": false, "error": ...}`, since the request itself succeeded.

## Routes

### Catchment

| Method | Path | Level | Description |
|---|---|---|---|
| GET | `/api/health` | none | Liveness check. |
| GET | `/api/catchment/identity` | read | The Catchment's name and id. |
| GET | `/api/catchment/settings` | read | Data root and cloud status. |
| PUT | `/api/catchment/settings` | full | Set the data root. |
| GET | `/api/catchment/duck-pools` | read | Built-in and defined pools. |
| POST | `/api/catchment/duck-pools` | full | Create or update a pool. |
| DELETE | `/api/catchment/duck-pools/{name}` | full | Remove a pool. |
| GET | `/api/catchment/compute-defaults` | read | Catchment-wide compute defaults. |
| GET | `/api/catchment/instance-types` | full | Available EC2 instance types. |
| POST | `/api/catchment/cloud/verify` | full | Check the cloud configuration. |
| POST | `/api/catchment/keys/rotate` | full | Replace API keys. |
| POST | `/api/catchment/reset` | full | Reset every Pond. |
| GET | `/api/catchment/usage` | full | Size of the state directory. |
| GET | `/api/catchment/archive` | full | Download the state directory as a tar stream. |

### Deploying and removing

| Method | Path | Level | Description |
|---|---|---|---|
| POST | `/api/deploy` | full | Deploy a Pond (a packaged upload or a git reference). |
| GET | `/api/ponds/{name}/versions/{version}` | read | Whether a version is deployed. |
| DELETE | `/api/ponds/{name}` | full | Remove a major line. |

### Status and history

| Method | Path | Level | Description |
|---|---|---|---|
| GET | `/api/status` | read | Every Pond's state, freshness, triggers and failures, the edges between Ponds, and the caller's access level. |
| GET | `/api/runs` | read | Run history, newest first. Parameters: `pond`, `lineage` (include upstream Ponds), `ripples` (include Ripple Runs), `limit` (up to 1000). |
| GET | `/api/view` | read | The Pond graph, including Ponds in upstream Catchments reached through ducts. |
| GET | `/api/lineage` | read | Observed table lineage. Parameters: `pond`, `major`, `table`, `columns`. |
| GET | `/api/ponds/{name}/trace` | read | The run that produced some rows. Parameters: `table`, `where`. |
| GET | `/api/ponds/{name}/freshness` | read | The Pond's current freshness. |

### Triggers and windows

| Method | Path | Level | Description |
|---|---|---|---|
| POST | `/api/ponds/{name}/tap` | demand | Tap. |
| POST | `/api/ponds/{name}/pulse` | demand | Pulse. |
| POST | `/api/ponds/{name}/wave` | demand | Set a Wave. |
| POST | `/api/ponds/{name}/tide` | demand | Set a Tide. Body: `bound_seconds`. |
| POST | `/api/ponds/{name}/untrigger` | demand | Remove the standing trigger. |
| GET | `/api/ponds/{name}/windows` | read | List windows. |
| POST | `/api/ponds/{name}/windows` | full | Add a window. |
| POST | `/api/ponds/{name}/windows/{window_name}/remove` | full | Remove a window. |

### Control

| Method | Path | Level | Description |
|---|---|---|---|
| POST | `/api/ponds/{name}/wake` | full | Wake. |
| POST | `/api/ponds/{name}/force` | full | Force. |
| POST | `/api/ponds/{name}/refresh` | full | Mark for a rebuild on the next run. |
| POST | `/api/repair` | full | Repair a connected set of Ponds. |
| POST | `/api/ponds/{name}/sleep` | full | Sleep. |
| POST | `/api/ponds/{name}/kill` | full | Kill. |
| POST | `/api/ponds/{name}/clear` | full | Clear a failure. |
| POST | `/api/ponds/{name}/reset` | full | Reset to the freshly deployed state. |
| POST | `/api/ponds/{name}/reset-contract` | full | Forget the recorded output schema. |
| POST | `/api/ponds/{name}/wipe-history` | full | Delete run history. |
| POST | `/api/ponds/batch` | full | Apply operations to many Ponds, as `duckstring do`. |
| GET | `/api/ponds/{name}/budget` | read | Retry budgets. |
| POST | `/api/ponds/{name}/budget` | full | Set retry budgets. |
| GET | `/api/ponds/{name}/duck` | read | Effective compute settings. |
| POST | `/api/ponds/{name}/duck` | full | Set or clear the compute override. |

### Data

| Method | Path | Level | Description |
|---|---|---|---|
| POST | `/api/query` | read | Run SQL against one Pond's tables. Body: `pond`, `sql`, optional `major`, `version`, `format` (`json`, `csv` or `parquet`). |
| POST | `/api/query/page`, `/api/query/count`, `/api/query/history` | read | Paged reads used by the web UI's data viewer. |
| GET | `/api/ponds/{name}/tables` | read | List published tables. |
| GET | `/api/ponds/{name}/ripples/{table}` | read | Download a table's published files as a zip. |
| DELETE | `/api/ponds/{name}/tables/{table}` | full | Delete a table. |
| GET | `/api/ponds/{name}/objects` | read | List Objects. |
| GET | `/api/ponds/{name}/objects/{obj}` | read | Download an Object. |
| DELETE | `/api/ponds/{name}/objects/{obj}` | full | Delete an Object. |

### Catalog

| Method | Path | Level | Description |
|---|---|---|---|
| GET | `/api/serve` | read | Served majors and exposed tables. |
| POST | `/api/serve/query` | read | Run SQL across the catalog. Read-level keys see only exposed tables. |
| POST | `/api/ponds/{name}/serve/promote` | full | Change the served major. |
| POST | `/api/ponds/{name}/serve/expose` | full | Expose or hide a table. |

### Spouts

| Method | Path | Level | Description |
|---|---|---|---|
| GET | `/api/ponds/{name}/spouts` | full | List Spouts. |
| POST | `/api/ponds/{name}/spouts` | full | Add a Spout. |
| POST | `/api/ponds/{name}/spouts/test` | full | Test a destination without writing data. |
| POST | `/api/ponds/{name}/spouts/{spout}/remove` | full | Remove a Spout. |
| POST | `/api/ponds/{name}/spouts/{spout}/{action}` | full | `wake`, `force`, `sleep`, `kill`, `clear` or `resync`. |

### Secrets and alerts

| Method | Path | Level | Description |
|---|---|---|---|
| GET | `/api/secrets` | full | Secret names and when each was set. Never values. |
| POST | `/api/secrets` | full | Set a secret. Body: `name`, `value`. |
| DELETE | `/api/secrets/{name}` | full | Delete a secret. |
| GET | `/api/alerts` | full | List channels. |
| POST | `/api/alerts` | full | Add a channel. |
| DELETE | `/api/alerts/{name}` | full | Remove a channel. |
| POST | `/api/alerts/{name}/test` | full | Send a test message. |
| GET | `/api/alerts/deliveries` | full | Recent deliveries. |

### Ducts

| Method | Path | Level | Description |
|---|---|---|---|
| GET | `/api/duct` | full | List ducts. |
| POST | `/api/duct` | full | Create a duct. |
| DELETE | `/api/duct/{origin}` | full | Destroy a duct. |
| POST | `/api/duct/{origin}/ponds` | full | Draw a Pond. |
| DELETE | `/api/duct/{origin}/ponds/{pond}` | full | Stop drawing a Pond. |
| POST | `/api/duct/{origin}/sync` | full | Draw every exposed Pond. |
| POST | `/api/ponds/{name}/open` | full | Open a Pond to demand from other Catchments. |
| POST | `/api/ponds/{name}/close` | full | Close it. |

The `/api/draw/*` routes that ducts use to transfer data, and the `/api/duck/*` and `/api/pool/*` routes that Ducks use to talk to the Catchment, are internal and not covered here.

## Metrics

`GET /metrics` serves Prometheus metrics without authentication, following exporter convention. Restrict network access to it if Pond names are sensitive.

| Metric | Type | Description |
|---|---|---|
| `duckstring_up` | gauge | 1 while the Catchment is serving. |
| `duckstring_pond_freshness_lag_seconds` | gauge | Seconds since each Pond's freshness. |
| `duckstring_pond_failed`, `duckstring_pond_blocked`, `duckstring_pond_killed` | gauge | 1 when the Pond is in that state. |
| `duckstring_pond_runs_completed_total` | counter | Completed Pond Runs. |
| `duckstring_pond_failures_total` | counter | Failed Pond Runs. |
| `duckstring_pond_run_seconds_total` | counter | Total Duck execution time, summed over Ripple Runs. |
| `duckstring_pond_duck_target` | gauge | 1 for the compute target each Pond runs on. |
| `duckstring_pond_flock` | gauge | 1 for each Pond's effective Flock mode and engine. |
| `duckstring_spout_delivery_lag_seconds` | gauge | Seconds since each Spout last delivered. |
| `duckstring_spout_failed` | gauge | 1 when the Spout's latest delivery failed. |
| `duckstring_alert_deliveries_total` | counter | Alert deliveries, by `status`. |
| `duckstring_flock_dispatched_total` | counter | Computations sent to the Flock, per Pond. |
| `duckstring_flock_dispatch_failures_total` | counter | Flock dispatches that failed and ran in the Duck instead. The only sign that a Flock is misconfigured. |

## Other interfaces

The catalog can also be queried over the Postgres wire protocol and Arrow Flight, each enabled by setting a port (`DUCKSTRING_SERVE_PG_PORT`, `DUCKSTRING_SERVE_FLIGHT_PORT`; see [Environment Variables](environment.md#serving)). Both use an API key as the password and apply the same access levels.
