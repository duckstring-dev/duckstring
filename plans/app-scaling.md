# App scaling: instances on other machines

Status: planned 2026-11-25, for 0.9.0, on branch `records-media` (worktree `../duckstring-records`). Follows
`plans/records-and-media.md`, which built `[app] instances` on the Catchment's own machine.

## Why

Duckstring is to be a data app factory: one Catchment per team, many Apps. An App outgrowing one machine, or a
Catchment kept small while its Ponds and Apps run on their own compute, needs App instances elsewhere: the same
launchers remote Ducks use, started and stopped by load.

It belongs in open source (the author's call, 2026-11-25): the launchers it reuses already are, and a self-hosted
"many Apps" Catchment without scaling would be incomplete at the point a team depends on it. What the hosted tiers
sell is not running it (machines, upgrades, backups, sign-in); fleet-wide policy, SSO and audit stay commercial.

## Decisions (2026-11-24/25)

- **Data goes direct; the Catchment carries requests and control.** A remote instance reads Pond data from the data
  root (its own DuckDB over the published Parquet, as `duckstring.app.pond()` does today: world attention's `wa_app`
  queries nothing through the Catchment), Records from PostgreSQL directly, and Media from an object store directly.
  Only the App's HTTP and WebSocket traffic (sign-in gate, sticky routing) and control pass through the Catchment.
- **Remote instances require** Records they can reach (an external PostgreSQL, or the managed one with `--listen`
  on an address the compute reaches; checked at start, refused with a clear message) and Media in an object store
  (a cloud data root always means a bucket exists). No Records or Media fallback through the Catchment. Remote
  instances also require `state = "none"` (a private state directory is one machine's).
- **Requests reach instances through a dial-back tunnel**: the agent dials the Catchment, as a Duck does, and holds
  one WebSocket carrying many byte streams. Worker machines keep no inbound rules, and it works on the jobs-based
  backends (Cloud Run, Container Apps) and behind the relay.
- **Placement is declared** like a Pond's: `[pond] duck = "M"`, a Pool, or dedicated, with `duck set` as the
  operator's override. `[app] instances` is the maximum, `min_instances` the warm floor (default 0).
- **The Catchment is the fallback.** With `min_instances = 0` and no remote instance ready, a request starts one on
  the Catchment (seconds), so a quiet App lives there and remote compute starts only under load; a failed remote
  start leaves the Catchment's serving. With `min_instances >= 1` the Catchment never runs the App: while the warm
  one starts, requests wait for it (up to the start timeout).
- **Concurrency is declared, load moves only the count.** `[app] concurrency` (default 4) is what one instance takes
  at once; the author sizes it with the instance type. Never derived from memory.
- **Connections:** a managed server's `max_connections` is sized from Records' memory; `[app] connections` is one
  instance's budget (default = `concurrency`). The supervisor won't grow an App past what is left, and each line's
  role gets `CONNECTION LIMIT` = `instances` x `connections` (plus a margin for `migrate`), so one App's pool can't
  starve another.
- **Fine control in four layers**, all for 0.9.0: settings with operator overrides; direct commands (scale, drain);
  the App reporting its own load; a replaceable scaler. Scheduled scaling is a scaler, not a built-in setting.

## The growth rule (the default scaler)

Grow by one (one start at a time) when, for `after` (10 s), any of these holds over the ready instances:

- active work per instance >= 70% of `concurrency`. Active = HTTP requests in flight, plus WebSockets that carried
  a message in the last `socket_idle` (5 s). An idle tab keeps its session and its instance (an instance with an
  open socket never idles out) but doesn't push growth;
- CPU >= 70% of the instance's cores;
- memory >= 80% of its limit (Streamlit-style Apps hold per-session state whether busy or not);
- the App's own reported load >= 0.7.

The 70% leads saturation because a remote start takes about a minute; the 10 s keeps a burst from starting a machine
that bills until it idles out.

**Routing packs.** A new session goes to the busiest ready instance still under `concurrency` and the memory
threshold, remote instances before the Catchment's; else the least busy. Sessions keep their instance by cookie.
Spreading would keep every instance slightly busy so none ever idled out after a peak.

**Shrinking.** An instance stops after `idle_timeout` with no requests and no open socket, never below
`min_instances`. Every stop drains: no new sessions, in-flight work finishes, then `SIGTERM`, then `SIGKILL` after
`[app] stop_grace` (default 10 s).

## Settings

```toml
[app]
instances = 8          # maximum (exists; > 1 needs state = "none")
min_instances = 0      # warm floor; >= 1 keeps the App off the Catchment
concurrency = 4        # active requests and sockets one instance takes
connections = 4        # PostgreSQL connections one instance may hold (default: concurrency)
stop_grace = "10s"

[app.scale]            # the default scaler's thresholds
requests = 0.7         # share of concurrency
cpu = 0.7
memory = 0.8
load = 0.7             # the App's own reported load
after = "10s"
socket_idle = "5s"
```

Operator overrides, kept across redeploys (a table keyed on the line, like `pond_duck`): `duckstring app scale APP
[--min N] [--max N] [--concurrency N] [--connections N] [--cpu F] [--memory F] [--clear]`. The App panel shows each
value and its origin (`pond.toml`, override, default).

The App's process gets `DUCKSTRING_APP_CONCURRENCY` and `DUCKSTRING_APP_CONNECTIONS`, and `app.records_pool()` sizes
a `psycopg_pool` pool from them (when `psycopg_pool` is installed).

## Commands and the App's own load

- `app scale APP --to N [--hold DURATION]`: set the count now; held until load would take it lower, or `--hold`
  runs out. Within `min_instances..instances`.
- `app instances drain APP ID`: stop new sessions to one instance, let its work finish, stop it.
- Routes for both (full), so the UI and scripts get them.
- The App reports its load (0 to 1) with a response header `X-Duckstring-Load` (stripped by the proxy) or
  `duckstring.app.report_load(x)` (a file in the instance's runtime directory, read each tick, by the supervisor for
  a Catchment instance and by the agent for a remote one). For bottlenecks no outside signal sees, such as
  `wa_app`'s one reader of the Sources at a time.

## The scaler interface

`DUCKSTRING_APP_SCALER=module:Class` (as `DUCKSTRING_DUCK_LAUNCHER`). Each tick, per App with instances or demand:

```python
class Scaler:
    def decide(self, app: AppView) -> Decision: ...

# AppView: key, limits (min, max, concurrency, thresholds, hold), now, recent decisions, and per instance:
#   id, where ("catchment" | "remote"), ready, starting, draining, requests, active_sockets, open_sockets,
#   cpu (share of its cores), memory (share of its limit), load (reported or None), age_s, idle_s
# Decision: desired (int), drain (instance ids), reason (str)
```

The supervisor enforces what must hold whatever the policy: the min and max, the connection budget, memory
reservations on the Catchment, one start at a time, drains finishing before stops, the fallback rules. A scaler that
raises is logged and the default rule decides that tick. The built-in rule is the default implementation.

## Mechanics

- **Launch.** Launchers gain a command override (module and extra arguments) beside the Duck's, carried in the
  `duck` dict, so every backend and the Pool agent can start `python -m duckstring.app_agent` instead of
  `duckstring.duck`. Instance keys are distinct from Pond keys (one launcher record per instance).
- **The App agent** (`duckstring.app_agent`): fetch the artifact over the Duck channel, build and switch to the
  App's environment (the Duck's `_use_pond_env` path), start `[app] command` with the same environment a Catchment
  instance gets (data root instead of the local layout, direct Records URL, object-store Media), report ready from
  `health` or the port, then serve the tunnel. Exits when the Catchment says stop, when the App exits (reported), or
  after `DUCKSTRING_DUCK_ORPHAN_MINUTES` without contact.
- **The tunnel.** One WebSocket from the agent to `/api/duck/{name}/{major}/app/{instance}/tunnel` (Duck token).
  Frames: stream id, type (open, data, close, window), payload; per-stream credit windows for backpressure; stream 0
  is control (agent to Catchment: ready, metrics, holds, load, exit; Catchment to agent: freshness, drain, stop).
  On the Catchment the tunnel opens a loopback port per remote instance, and each connection to it becomes a stream
  the agent connects to the App's port, so the proxy's HTTP and WebSocket forwarding works unchanged against
  `Instance.port`. A dropped tunnel fails its streams and marks the instance not ready; the agent reconnects with
  backoff; past the silence window the instance is terminated through its launcher.
- **Catchment restarts.** A remote instance's `app_instance` row stays open; an agent reconnecting within the grace
  is adopted when it runs the current deploy, else told to stop.
- **Freshness and leases.** A remote instance reads the data root, which trails the Catchment's local publish by the
  Persist upload. Its freshness (the control channel's, written by the agent where the handle reads it) and the read
  leases its requests take use the Sources' persisted positions, and retention keeps those versions in the durable
  layer too (the sidecar's `v_floor` and `gc_tiers` follow `retain_from`; verify, and test).
- **Records over TLS, verified.** Remote URLs today use `sslmode=require` (encrypted, unverified). Instances and
  remote Ducks get the certificate (PEM in their environment, written to a file) and `sslmode=verify-full`; the
  managed certificate names the `--host` address. Remote Ducks also read a listening managed PostgreSQL directly,
  with the Duck channel only for one on loopback.
- **Metrics.** CPU and memory of an instance's process tree with `psutil` (a new core dependency), on the Catchment
  for its instances, from the container's cgroup or `psutil` in the agent for remote ones, sent every 2 s.
- **Execution limits.** A launcher may declare a maximum lifetime (Container Apps' and Cloud Run's 7-day jobs); the
  supervisor drains and replaces an instance an hour before it.
- **Shutdown.** Catchment shutdown drains then stops every remote instance (`shutdown_all`), as for Ducks.

## Observability

- `/metrics`: per App instances by place, active requests and sockets, CPU, memory, reported load; starts and stops
  by reason.
- Each start and stop records its reason (`app instances`, the App panel): "started: memory 83% on #2".
- An App held at its maximum under pressure shows a warning on its node and in `/api/status`.
- An opt-in alert event kind `app_scale` (outside `all`, like `openlineage`).

## Phases

1. Mechanics: command override, agent, tunnel, remote instance, adoption, freshness and leases, verified TLS, a
   test backend (the agent as a subprocess with its own root, through the tunnel, `on_this_machine` false).
2. Scaling: metrics, active sockets, the scaler interface and default rule, packing, the fallback, `min_instances`,
   settings and overrides, commands, the App's load, connection budgets and sizing, observability, UI.
3. Cloud validation with the author's go-ahead (cost): Fargate first, then Container Apps and Cloud Run; docs facts
   into `plans/0.9-docs.md`.

Not planned: scheduled scaling as a setting (a scaler), a connection pooler (PgBouncer), direct inbound networking
to instances (a possible later optimisation inside a VPC), presigned Media links for large downloads (worth adding
to `duckstring.app` later).
