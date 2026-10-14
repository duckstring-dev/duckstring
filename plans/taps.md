# Taps: freshness that follows usage

Status: **planned, not built.** Agreed in outline with the author on 2026-10-14, as the second stage after
`plans/refresh-triggers.md` (which it depends on: Refresh, demand, and the floating refresh's clock).

## Why

A query is a demand signal. While an app is being used its data should stay fresh, and when nobody is using
it the pipeline should go quiet, with no schedule to tune. That is pull orchestration carried to the
consumer's edge, and no scheduler offers it.

It belongs to the data engineer. An app could send a refresh with every query, but then freshness policy is
scattered through app code, and every app needs a key that can refresh anything. So it is configured in
Duckstring, on the access point the app uses.

## What exists today

`pond_open.tap_on_get` (migration `002`, `duckstring duct open --tap-on-get`) fires `Driver.tap` when
`/api/query` or `/api/query/page` reads an open Pond (`routes/data.py:_maybe_tap_on_get`), after serving the
current snapshot. Two problems:

- **It can't tell readers apart.** The web UI's data viewer reads through `/api/query/page`, so inspecting a
  Pond inside Duckstring triggers runs, and a notebook that is happy with stale data triggers them too.
- **It is a Tap, so it goes with pull** (`plans/refresh-triggers.md`), and it isn't reachable from the
  Postgres and Flight wires (`catchment/pg_wire.py`, `catchment/flight_sql.py`).

## The model

A **Tap** is a named access point on a Pond (one major line) for an app to query. Spouts push a Pond's data
out; Taps let apps draw it.

- **Scope**: the Pond, optionally limited to some of its serviceable tables (default: all of them; a Tap
  never widens what the serving core exposes).
- **Credential**: its own key, minted when the Tap is added and shown once (as `auth.generate` does). A query
  is classified by the credential it arrives with: the Tap's key means the Tap's app. The web UI, the CLI
  and every other key never match a Tap, so they never trigger runs. The key can read the Tap's scope and
  cause only the Tap's refreshes, so an app can't refresh anything else.
- **Policy**: `--fresh-within D`, **default 0**.
- **Wait**: by default a query is served immediately from the current snapshot; `--wait [TIMEOUT]` makes it
  wait for the refresh it caused to land (or time out and serve what there is).

### Freshness policy

`--fresh-within D` uses the floating refresh's clock (`--every`), evaluated on each query instead of on a
tick. A query through the Tap sends a one-shot refresh when the most recent demand on the Pond, or with none
pending its freshness, is older than `D`:

```
on a query through a Tap with fresh_within D:
    ref = max(Pond.demand) if Pond.demand has a real entry else Pond.startF
    if now + Pond.D - ref >= D:
        refresh_now(Pond)                 # demand at now, marked as a trigger's (from_refresh)
```

- `--fresh-within 0` refreshes on every query. Queries in the same instant collapse (one demand), and while
  a run is in flight the throttle paces the rest to the pipeline's bottleneck, as for any fast refresh.
- `--fresh-within 1d` behaves like `refresh add POND --every 1d` while the app is in use, and does nothing
  while it isn't.
- Any other demand on the Pond (a manual refresh, a standing refresh) counts as recent demand, as it does for
  `--every`, so a Tap never adds runs a standing refresh already covers.
- The data a query sees is at most `D` plus the pipeline's run time old while queries keep coming
  (`--wait` makes the query itself see the result of its own refresh).

Because the refresh is trigger demand on the Pond, everything else applies unchanged: the throttle, rule 6,
demand to required Sources only, and the minted epoch at the Inlets.

## Where queries arrive

- **HTTP**: the `/api/query` family. With a Tap key, the serving connection is the Tap's (below), and the
  request names no Pond (the key implies it).
- **Postgres wire**: the password is the Tap's key, as it is an API key today.
- **Arrow Flight**: `authorization: Bearer <tap key>`.

All three resolve the credential through `auth`, which learns a fourth kind of principal besides the
read/demand/full ladder: a Tap principal, `(pond_key, tap_name, tables)`. `audit_routes` classifies the query
routes as accepting it; every other route rejects a Tap key (401).

Each Tap gets a serving connection limited to its scope: `serving.build_serving_con` with a scope filter
(sandboxed, materialised local tables, external access locked, only the Tap's Pond schema and tables), cached
per Tap in `ServingCache` and refreshed on `data_version` as the shared one is.

## Persistence (migration)

- `pond_tap` (PK `(pond_id, name)`): `key_hash` (sha256, as `catchment_key`), `fresh_within_ms` (default 0),
  `wait_ms` (NULL = serve immediately), `created_at`, `last_query_at`, `query_count`.
- `pond_tap_table` (`(pond_id, tap_name, table_name)`): the table subset; no rows means all serviceable
  tables.
- `pond_open.tap_on_get` is dropped. `pond_open` stays as the duct's producer-side gate.

`last_query_at` and `query_count` are written in batches (not per query) so a busy app doesn't serialise on
the Catchment's SQLite lock; they are observability only.

## Surface

- **CLI** (`group verb POND`, as `spout`): `duckstring tap add POND --name NAME [--tables a,b]
  [--fresh-within D] [--wait [TIMEOUT]]` prints the key once; `tap ls POND` (name, scope, policy, last query,
  query count); `tap rm POND NAME`; `tap rotate POND NAME` mints a new key.
- **HTTP** (full, since a Tap is a credential): `GET/POST /api/ponds/{name}/taps`, `DELETE
  /api/ponds/{name}/taps/{tap}`, `POST .../taps/{tap}/rotate`. The query routes accept the Tap key.
- **Web UI**: a Taps section in a Pond's sidebar, beside Spouts: the list (policy, "last query 2 min ago"),
  an add form (name, tables, fresh within, wait) that shows the key once with a copy button, rotate and
  remove. On the canvas, a Tap is a small node on its Pond, like the refresh node, since both are sources
  of demand; one node per Pond with a count badge.
- **Metrics** (`/metrics`): `duckstring_tap_queries_total{pond,tap}` and
  `duckstring_tap_refreshes_total{pond,tap}`. Status: `/api/status` lists each Pond's Taps (name, policy,
  last query), never keys.

## Interplay

- **Multiple majors**: a Tap belongs to one `name@major` line; removing the line removes its Taps.
- **Triggers on Outlets**: a Tap on an Outlet is the natural case, but any deployed Pond can have one, as
  any can have a refresh.
- **Failed or blocked Ponds**: a query is still served (the last good snapshot); the refresh is refused at a
  blocked Pond as any demand is, and re-sent on unblock (rule 8).
- **Ducts**: unchanged. A downstream Catchment's Draw carries demand up the duct (`/refresh?at=`); a Tap is
  for apps.
- **Sleep and Kill** don't disable Taps; queries keep being served, and a slept Pond takes new demand again,
  as it would from a refresh. Removing the Tap stops its demand.

## Docs

- Concept: the Orchestration page gains "On request": freshness that follows usage, in a few sentences.
- Guide: "Serving apps with Taps" under Getting Data Out (an app over `reports`, its key, `--fresh-within`,
  connecting through Postgres or HTTP), next to Querying.
- Reference: `reference/cli/tap.md`, the HTTP API route table, `/metrics` families, the auth page (the Tap
  principal).
- CLAUDE.md: a Taps section (model, policy clock, auth principal, serving scope).
- Landing page: a candidate line under the pull-orchestration pillar, if the author wants it there.

## Playground

Optional, and a strong tour moment: a toggle on an Outlet, "an app is using this", that sends a query every
second while on. Turning it on shows the pipeline waking and keeping pace; turning it off shows it going
quiet. It needs only the demand-at-now path the playground already has, plus the `--fresh-within` clock.

## Tests

- Engine-level: the policy clock (fires when demand is older than `D`, not when recent demand exists, `0`
  every query, same-instant queries collapse), under the throttle with a burst of queries.
- Auth: a Tap key reads only its scope, triggers only its Pond, is rejected elsewhere; rotation; open mode.
- Serving: the scoped connection exposes only the Tap's tables; the pg and Flight wires classify by key.
- Driver: `pond_tap` CRUD, reload, the counters' batching, removal with the line.
- The web UI data viewer and CLI reads never send demand (regression for today's tap-on-get).

## Ship order

1. After `plans/refresh-triggers.md` lands.
2. Auth principal, `pond_tap`, Driver CRUD, CLI and HTTP management.
3. The policy on the HTTP query routes, then the pg and Flight wires.
4. Web UI, metrics, docs, and the playground toggle.

## Open questions

- **Several Ponds per Tap**: an app often reads more than one Pond over one connection. Allow a Tap to cover
  several Ponds (each refreshed by its own clock), or keep one Pond per Tap and let an app hold several keys?
- **`tap_on_get` migration**: drop it with a changelog note (it fired on any reader, including the web UI),
  or convert each open Pond with it set into a Tap whose key the operator then rotates to obtain?
- **Wait timeout default**, and whether `--wait` should be allowed with a non-zero `--fresh-within`.
- **Rate limiting** per Tap, or is the throttle enough (it bounds runs, not query load on the serving core)?
- ~~The policy clock~~: settled 2026-10-14. `--fresh-within` and `--every` share one clock, the later of the
  newest request and the Pond's freshness, so neither refreshes data something else already keeps fresh.
