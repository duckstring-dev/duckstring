# Alerts: failure & freshness notifications to the channels a team already runs

Status: **built** (webhook + email channels, event-driven failure/contract/spout/recovery alerts with
root-cause dedup, tick-driven freshness-SLA breaches, the outbox worker, CLI + full-gated API, **and full
UI management** — the catchment-wide **Alerts** menu beside Secrets + a per-Pond **Alerts** section in the
Sidebar — tests in `tests/test_alerts.py`). Deferred: re-notify cadence, a `/metrics` endpoint. The
last observability gap before Duckstring is credible for serious
data engineering: when a pipeline breaks — or, more insidiously, goes *stale without breaking* — the
right people learn about it, on the channel they already watch (email, Slack, PagerDuty), without polling
the UI. Alerting is **observability, not data movement**: it shares no code with egress, but it reuses
egress's *shape* wholesale (operational config, a scheme-selected driver seam, `${env:}`/`${secret:}`
credentials, an async delivery worker that never cascades a failure back into the engine).

## Positioning — alerts are the observability sibling of a Spout

A **Spout** binds a Pond to an external *data* system. An **alert channel** binds the Catchment (or one
Pond) to an external *signalling* system. Almost every design decision egress settled transfers verbatim,
and that consistency is the point — an operator who has configured a Spout already knows how to configure
an alert:

- **Operational config, not `pond.toml`.** Channels and their credentials are environment-specific (a dev
  Catchment must not page prod on-call). Created via CLI/API, persisted in `duck.db`, survive redeploys —
  the Window/Spout rule.
- **Destination is a URI whose scheme picks a driver.** `https://…` / `http://…` (a generic webhook,
  Slack-incoming-webhook compatible), `mailto:…` (SMTP). The pluggable *notifier* has **no product noun**,
  exactly like the egress driver.
- **Credentials via `${env:NAME}` / `${secret:NAME}`, resolved at send time.** Reuses
  `egress/credentials.py` and the write-only secret store unchanged — a Slack webhook URL or an SMTP
  password is just a `${secret:}`.
- **Delivery failure never cascades.** A channel that 500s fails *the notification* (logged in an outbox,
  retried), never the Pond — the same discipline as the egress worker failing the Spout, not the source.

**What alerts deliberately are *not*:** a routing-rules engine, an escalation-policy / on-call-schedule
system, an incident tracker. That is PagerDuty/Opsgenie's job, and reimplementing it would violate the
brand rule of naming the gap honestly and integrating with what teams already run (the same philosophy as
egress's "get my data where my consumers already are"). Duckstring **emits good events and delivers them**;
the generic webhook is the highest-leverage channel precisely because it lets a team route into whatever
they already operate.

### A channel is NOT an engine node

A Spout is a real engine node because it has freshness/delivery/run semantics (it's the dual of a Draw). A
notification has none of that — it is fire-and-(retry-until)-forget. So a channel is deliberately
**lightweight config + an outbox + a worker**, not a `pond`/`pond_state` node. This keeps the engine (and
`theory.md`'s state machine) untouched: alerting observes state transitions, it does not participate in them.

## The two firing mechanisms

Alerting hangs off state the engine *already* computes; it adds no new orchestration state.

**Event-driven** (fired inline from `Driver` state transitions, cheap — failures are rare):

| Event kind | Fired when | Severity |
|---|---|---|
| `failure`  | a Pond Run gives up (retries exhausted / dead-or-silent Duck / Duck-level error) | error |
| `contract` | the Duck refuses to publish — output broke the major line's additive contract | error |
| `spout`    | a Spout delivery fails (terminal — blocks nothing, but the last mile is down) | error |
| `recovery` | a previously-`failed` Pond **or** Spout clears (a fresher run, or a manual clear) | info |

**Tick-driven** (evaluated in `scheduler_tick`, alongside the liveness sweep — the same shape):

| Event kind | Fired when | Severity |
|---|---|---|
| `freshness` | a scoped Pond's staleness exceeds the channel's bound (`--stale 1h`) | warning |
| `recovery`  | a stale Pond's freshness advances back under the bound | info |

**Freshness is the headline.** A pipeline can be green with zero failures and still *wrong* — nothing
triggered it, or an upstream is quietly slow, and an Outlet is hours stale under a dashboard. Freshness is
Duckstring's whole model (`staleness = now + D - F`), so a freshness-SLA alert is something the
package-graph world expresses cleanly and is what actually keeps data engineers up at night. Failure alerts
are the easy companion.

## Root-cause dedup (the anti-storm rule)

The classic way alerting fails is the storm: one failed Source blocks 20 downstream Ponds and pages you 21
times. The engine already distinguishes the **failed** root (`is_failed`) from the **blocked** propagation
(`is_blocked`, derived downstream by `derive_blocked`) — a blocked Pond is *never* `is_failed`, and we only
ever call the fail path on the root. So failure alerts **naturally fire only for roots**; blocked Ponds get
no alert. The failure payload carries the **blast radius** (the currently-blocked downstream Pond names) as
context, so one alert conveys the whole impact.

Within a root, dedup is by **episode**: the delivery outbox has `UNIQUE(channel_id, dedup_key)` with
`dedup_key = "{kind}:{pond}:{f}"`. Retries at the same failed freshness → one alert; a *new* failed
freshness (a fresh on-change run that also failed) → a new alert. Recovery is its own `dedup_key`
(`recovery:{pond}:{f}`), so a fail→clear→fail cycle notifies correctly.

**Re-notify cadence for a standing breach is deferred** (v1 fires once per breach episode + once on
resolve). A periodic re-page for an unacknowledged freshness breach is the natural extension — a
`renotify_after` on the channel that buckets the dedup key by time — but it needs an ack/silence concept to
not be annoying, and that is PagerDuty's territory. Documented, not built.

## Data model (migration `014_alert.sql`)

- **`alert_channel`** — the binding. `id`, `name` (unique), `destination` (the URI, `${…}` refs intact),
  `scope_pond_name_id` (FK → `pond_name`, NULL = catchment-wide), `events` (CSV of kinds, or `all`),
  `stale_ms` (NULL, or the freshness-SLA bound), `enabled`, `created_at`. Scoped by **pond *name***, not the
  selected `pond` row, so a channel survives version/major changes.
- **`alert_delivery`** — the outbox + dedup ledger + delivery log in one. `id`, `channel_id` (FK),
  `dedup_key`, `event_kind`, `pond_name` (NULL for catchment-wide), `severity`, `payload` (JSON — the
  rendered `AlertEvent`), `status` (pending/sent/failed), `attempts`, `error`, `created_at`, `sent_at`.
  `UNIQUE(channel_id, dedup_key)` is the fire-once fence. Doubles as observability (`alert log`).

## The notifier seam (`alerts/`, mirrors `egress/`)

```python
class Notifier(Protocol):
    def send(self, event: AlertEvent) -> None    # deliver; raise (sanitised) on failure
    def test(self) -> None                        # probe connectivity/creds, deliver nothing real
```

`get_notifier(destination)` resolves the driver by the URI scheme (a `_REGISTRY`, exactly like
`get_egress`); an unknown scheme raises with the built list. `AlertEvent` is the rendered payload
(`kind`, `severity`, `pond`, `f`, `title`, `message`, `detail`, `catchment`, `ts`). Bundled drivers:

- **`WebhookNotifier`** (`http`/`https`) — POSTs a JSON body that is both a plain structured event *and*
  Slack-incoming-webhook compatible (a top-level `text` summary Slack renders, plus the structured fields).
  This one integrates with everything: Slack, generic webhook receivers, PagerDuty Events API via a proxy.
  Credentials (a signing token in the URL) resolved at send.
- **`EmailNotifier`** (`mailto:to@host?from=…&smtp=host:port&tls=1`) — stdlib `smtplib` + `email.message`,
  SMTP host/port/user/pass from the URI query (creds as `${env:}`/`${secret:}`) or `DUCKSTRING_SMTP_*` env.
  The simplest floor the prompt asked for.

Payloads reuse what `/api/runs` surfaces (error message, freshness, pond) but are **sanitised** — the same
concern behind `_redact_tracebacks`: a channel destination can be third-party, so the outbound message
carries the error *message* and never a raw traceback (which can leak paths/connection strings).

## Delivery — the alert worker (`catchment/alert_worker.py`)

An async loop in the Catchment process, the exact shape of `egress_worker`: woken on `Driver._signal_alert`
(a new delivery was enqueued) or a 5 s self-healing tick. Each pass it drains `pending` `alert_delivery`
rows, resolves each channel's notifier, `send`s in a threadpool with a per-send timeout, and marks the row
`sent` (with `sent_at`) or bumps `attempts`/records `error`. A row that exceeds `MAX_ATTEMPTS` is parked
`failed` (visible in `alert log`) so a permanently-broken channel stops retrying but the failure is
auditable. **A send exception is caught and recorded — it never propagates into the engine.**

`Driver._emit_alert(kind, pond, severity, detail)` is the enqueue seam: it finds enabled channels matching
the scope (catchment-wide, or this pond's name) and event filter, renders an `AlertEvent`, and
`INSERT OR IGNORE`s one `alert_delivery` per channel (the dedup fence), then signals the worker. It is
wrapped so a bug in alerting can never break a Pond Run.

## CLI / API surface

- `duckstring alert add --to <uri> [--pond NAME] [--on failure,recovery,…|all] [--stale 1h] [--name N]`
- `duckstring alert ls` — channels + enabled/scope/events/bound
- `duckstring alert rm NAME`
- `duckstring alert test NAME` — send a test notification through the channel (validates creds/connectivity)
- `duckstring alert log [--limit N]` — recent deliveries (kind, pond, status, error) — the delivery audit
- `/api/alerts` GET/POST, `/api/alerts/{name}` DELETE, `/api/alerts/{name}/test` POST,
  `/api/alerts/deliveries` GET — **all `full`-gated** (a channel destination is an egress surface).

## Reuse, non-goals, risks

- **Reuse**: `egress/credentials.py` (`${env:}`/`${secret:}` resolution) and the secret store verbatim; the
  driver-seam/registry pattern from `egress/base.py`; the async-worker + `_signal_*` wake pattern from
  `egress_worker`; `is_failed`/`is_blocked`/`derive_blocked` for root-cause dedup; the `scheduler_tick`
  liveness-sweep shape for the freshness tick.
- **Non-goals** (v1): re-notify cadence / ack / silence (→ PagerDuty), escalation policies, on-call
  schedules, per-severity routing beyond the event-kind filter, a `/metrics` Prometheus endpoint (a
  separate, easy follow-up — self-hosters plug their own Grafana/Alertmanager into gauges; hosted
  dashboards = cloud).

## UI (built)

Channels are managed from two places (both **full access only** — the routes are `auth.full`), reusing one
`AlertChannelForm` + `ChannelRow` (`frontend/src/components/AlertsMenu.tsx`):

- **Catchment-wide** — an **Alerts** button beside **Secrets** in the top-right `ControlsPanel`
  (`DagCanvas`) opens `AlertsMenu`: every channel (scope/events/SLA + destination, a per-channel **test**
  and remove), an add form (destination, optional name/scope, event-kind chips, freshness-SLA input), and a
  **Delivery log** tab (`GET /api/alerts/deliveries`) — the audit trail with per-row status/error.
- **Per-Pond** — an **Alerts** `Section` in the Sidebar's pond panel (`AlertEditor`, after Spouts) lists the
  channels scoped to that Pond and adds one pinned to its name (`fixedScope`). Keyed by pond id so a
  selection switch remounts it fresh.

Consistent with the read-mostly UI, alert config is the exception it shares with Spouts/Secrets/Windows:
operational, mutable, full-gated. `frontend/src/lib/api.ts`: `fetchAlerts`/`addAlert`/`removeAlert`/
`testAlert`/`fetchDeliveries`. A `test` result is data (`{ok}`/`{ok,error}`), rendered inline, never a throw.
- **Risks**: a flaky channel retrying forever (bounded by `MAX_ATTEMPTS` → parked `failed`); a slow
  destination starving the worker (bounded by a per-send timeout, like egress); freshness false-positives on
  never-run or windowed Ponds (v1 skips never-run Ponds; windows refine `D` — start with `now - end_f`).

## Testing

- Channel CRUD + persistence + restore across restart (mirrors the spout/window tests).
- Event routing + dedup: a failed Pond enqueues exactly one delivery per matching channel; a blocked
  downstream Pond enqueues none (root-cause dedup); a fail→clear emits a `recovery`; scope + event filter
  select the right channels.
- Webhook delivery against a captured payload (a local receiver / a stub notifier): the Slack-compatible
  `text` is present and the traceback is absent (sanitised).
- Freshness SLA: a Pond driven past its `stale_ms` in sim-time enqueues one `freshness` breach, then a
  `recovery` when it advances.
- Worker: a failing notifier bumps `attempts` and parks `failed` at the cap — and never fails the Pond.
- `ruff check .` clean.

## Open questions for later

- `/metrics` (Prometheus gauges: failure rate, freshness lag, delivery lag) — same milestone or a fast
  follow? Leaning follow-up; the seam here doesn't block it.
- Re-notify cadence — worth it once there's an ack surface; otherwise noise. Deferred.
- A first-class **freshness monitor** decoupled from a channel (so multiple channels share one SLA
  definition) vs. the v1 per-channel `stale_ms`. Per-channel is simpler and ships; revisit if operators
  want one SLA fanning out to several channels.
