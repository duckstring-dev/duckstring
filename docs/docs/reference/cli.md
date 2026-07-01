---
title: CLI
description: Full command reference for the duckstring / ds CLI.
---

# CLI Reference

The CLI installs as both `duckstring` and `ds`; the two are identical. Every command and group prints detailed help with `--help`, `duckstring --version` prints the installed version, and shell completions install with `duckstring --install-completion`.

## Common options

Most commands that talk to a Catchment share these:

| Option | Meaning |
|---|---|
| `--catchment`, `-c {name}` | Target a registered Catchment (default: the configured default; if exactly one is registered, it's implicit) |
| `--major`, `-m {int}` | Target a specific major version line (default: the highest deployed) |
| `--version`, `-v {semver}` | Target a specific version, e.g. `1.2.3` — must be its major line's currently selected version |
| `--silent` | Submit without opening the live status view |
| `--watch` | Keep the status view open even after a one-shot settles |

## `duckstring catchment` — work with Catchments

| Command | Description |
|---|---|
| `catchment init -n {name} [--host H] [-p PORT] [--root DIR] [--key KEY \| --generate-key] [--header 'N: v']… [-y]` | Create and register a local Catchment, then start its server. Defaults: host `127.0.0.1`, port `7474`, root `~/.duckstring/{name}`, no API key (open). `--generate-key` mints the read/demand/full key ladder, prints all three once, and stores the full key (mutually exclusive with `--key`, which sets a single full-access key). Offers to set as default (`-y` accepts). |
| `catchment start {name}` | Start the server for a registered local Catchment. |
| `catchment rotate-keys [-c NAME] [--level read\|demand\|full]… [-y]` | Reroll a Catchment's access keys (default all three; `--level` repeatable for a subset), printing the new keys once. The old key for each rerolled level stops working; the internal Duck token is untouched. If the full key is rerolled, the stored registration is updated. Requires a full-access key. |
| `catchment connect -n {name} --path {url} [--key KEY] [--header 'N: v']… [-y]` | Register a remote Catchment by URL; `--key` stores its API key (sent as a Bearer header — use a `demand` key for a downstream that only solicits and draws), `--header` stores arbitrary headers for platform auth (e.g. `'Authorization: Key …'` for Posit Connect) — both attached to every request. |
| `catchment list` | List registered Catchments; `●` marks the default. |
| `catchment download [-c NAME] [--path DIR] [-y]` | Download the Catchment's entire root (database, artifacts, data, ledgers) into a local directory — default `./.duckstring`, so it drops straight into a platform deploy bundle. Shows the state size and asks before transferring (`-y` skips); streams with a progress bar. |
| `catchment set-default {name}` | Set the default Catchment. |
| `catchment disconnect {name} [--purge]` | Unregister; for local Catchments, offers to delete the data directory (`--purge` deletes without asking). |
| `catchment open {pond} [-m M] [--tap-on-get]` | Mark a Pond open to demand from any source; `--tap-on-get` makes a [query](../guides/querying-data.md) read fire a Tap (snapshot served first). |
| `catchment close {pond} [-m M]` | Remove a Pond's open flag. |

Registrations and the default live in `~/.duckstring/config.toml`.

### `duckstring catchment duct` — draw Ponds from other Catchments

Conduits that draw a Pond from an upstream Catchment into the consuming one (`-c`, default). See [Connecting Catchments](../guides/connecting-catchments.md). `{upstream}` is a registered Catchment name.

| Command | Description |
|---|---|
| `catchment duct create {upstream} [--sync] [-c]` | Open a duct from `{upstream}` into the consuming Catchment (forwards the upstream's URL, credentials, and identity). `--sync` then draws every Pond it exposes. |
| `catchment duct destroy {upstream} [-c]` | Remove a duct and all the Pond Draws it created. |
| `catchment duct add {upstream} {pond} [-m M] [--incremental] [-c]` | Draw one upstream Pond (materialises a Pond Draw). `--incremental` is reserved for delta transfer (not yet implemented). |
| `catchment duct remove {upstream} {pond} [-m M] [-c]` | Stop drawing a Pond. |
| `catchment duct sync {upstream} [-c]` | Draw every Pond the upstream currently exposes. |
| `catchment duct ls [-c]` | List ducts and the Ponds each draws. |

## `duckstring pond` — manage Pond projects

| Command | Description |
|---|---|
| `pond init {name}` | Scaffold a new Pond project in the current (empty) directory. |
| `pond demo [--ripple \| --trickle]` | Create a four-Pond demo pipeline as subdirectories. Default (or `--ripple`): the overwrite-Ripple set (`transactions`, `products`, `sales`, `reports`). `--trickle`: the incremental-[Trickle](../guides/trickle.md) set (`orders`, `catalog`, `priced`, `revenue`). |
| `pond hydrate [-s SOURCE] [--from-catchment] [-c NAME]` | Materialise the project's [Puddles](../guides/local-testing.md) into `puddles/`. Sources without a definition are skipped with a warning; `--from-catchment` fills them from the Catchment's exported tables; `-s` restricts to specific Sources. |
| `pond run [--ripple NAME] [--fresh]` | Execute the Pond locally against its hydrated Puddles, output to `puddles/out/`. `--ripple` runs a single Ripple against the last run's state; `--fresh` ignores a self-puddle seed. |
| `pond deploy [-c NAME] [--git REF] [-y] [--all]` | Deploy the current Pond project (reads `pond.toml`). `--all` deploys every subdirectory containing a `pond.toml`; `--git` deploys from a git ref (branch/tag/commit) of the project's `origin` remote instead of uploading the working tree; `-y` skips confirmations. |

## `duckstring puddle` — inspect local test data

See [Local Testing](../guides/local-testing.md). All three operate on the current project's `puddles/` directory, no Catchment involved.

| Command | Description |
|---|---|
| `puddle ls` | List hydrated Puddles and run output, with row counts, size, and age. |
| `puddle show {pond}.{table} [-n N]` | Preview a table (run output wins when a self-puddle shares the name). |
| `puddle query {sql}` | Run SQL across everything local — snapshots as `"{source}"."{table}"`, output under the Pond's own name. |

## `duckstring trigger` — demand signals

See [Triggers](../guides/triggers.md) for semantics.

| Command | Description |
|---|---|
| `trigger tap {pond}` | Pull once — a single resupply from Sources. |
| `trigger pulse {pond}` | Push once — run the lineage through to the Pond, to now. |
| `trigger wave {pond}` | Standing pull — free-run at the bottleneck's pace. |
| `trigger tide {pond} {bound}` | Standing push — keep staleness under `bound` (compound durations: `30s`, `90m`, `1d`, `1h30m`). |
| `trigger remove {pond}` | Remove the standing Wave/Tide (existing work drains). |

One-shots (tap/pulse) open the live status view and close when the target settles; standing triggers keep it open until `Ctrl+C` (the trigger persists).

### `duckstring trigger window` — availability windows

See [Windows](../guides/windows.md). The Pond name comes directly after `window`:

| Command | Description |
|---|---|
| `trigger window {pond} add -n {name} -e {every} [-s START] [-d DUR] [-o DAYS] [-u UNTIL]` | Add a recurring window. `--every` is a single-unit interval (`10s`, `12h`, `1d`, `1w`); `--start` is ISO 8601 or `HH:MM` UTC (default `00:00` today); `--duration` accepts compound durations and defaults to `--every` (back-to-back); `--on` restricts weekdays (`MON,WED,FRI`); `--until` expires the rule. |
| `trigger window {pond} list` | List the Pond's windows. |
| `trigger window {pond} remove {name}` | Remove a window rule. |

## `duckstring spout` — egress bindings

Publish a Pond's output to external systems. A Spout is operational config (persisted, survives redeploys), not declared in `pond.toml`. Credentials go in the destination URI as `${env:NAME}` (process environment) or `${secret:NAME}` ([secret store](#duckstring-secret--credential-store)) references, resolved only at egress time — never stored in the binding or logged. After each successful Pond Run, the egress worker delivers the Pond's published tables to the destination as snapshot Parquet (`{prefix}/{table}.parquet`).

`file://`, `s3://`, `gs://`, and `postgres://` work today. Object-store credentials go in the URI query: `s3://bucket/prefix?key_id=${env:AWS_KEY}&secret=${env:AWS_SECRET}&region=us-east-1` (also `endpoint`, `url_style`, `use_ssl`, `session_token`); `s3://` with no key falls back to the AWS credential chain (env / instance profile); `gs://` needs HMAC `key_id`+`secret`.

`postgres://user:${env:PGPASS}@host/db?schema=public` syncs **incrementally**: a [merge Trickle](../guides/trickle.md)'s changelog applies as upserts + deletes inside one transaction, exactly-once. A transactional destination **requires a primary key**, so the source table must be a merge Trickle — a plain/overwrite table is refused at creation with a signpost error.

| Command | Description |
|---|---|
| `spout add {pond} --to {uri} [--table T \| --all] [--mode auto\|full\|append] [--name N]` | Bind a Spout. `--to` is a `file://`/`s3://`/`gs://`/`postgres://` URI (credentials as `${env:NAME}`); `--table` egresses one table, default all; `--mode` defaults `auto`; `--name` defaults to the table (or scheme), `-2`/`-3` on collision. |
| `spout ls {pond}` | List the Pond's Spouts with their delivery watermark and state (ok / retrying / failed). |
| `spout rm {pond} {name}` | Remove a Spout. |
| `spout resync {pond} {name}` | Force a full re-egress (clears the watermark + any failure). |
| `spout sleep \| wake {pond} {name}` | Disarm / re-arm the Spout's standing Wake (it delivers on each source advance). |
| `spout force {pond} {name}` | Re-arm and re-deliver the current freshness now. |
| `spout kill \| clear {pond} {name}` | Park the Spout (terminal) / clear a failed-or-killed Spout. |

A Spout is a **real Pond** hanging off its source with a standing **Wake** (the egress dual of a [Pond Draw](../guides/connecting-catchments.md)) — it delivers whenever the source's freshness advances, never pulls on the source, and never blocks anything (its runs and failures are its own, with full run history + tracebacks in [run history](#duckstring-status)). The **Control** verbs above apply to it; the **Demand** verbs (tap/wave/pulse/tide) do not. To **throttle** delivery to a cadence, put a [window](#duckstring-trigger--demand-triggers) on the Spout — it's a Pond, so `trigger window {source}#{spout} add -e 1h …` (or the UI) works directly: it delivers at most once per window.

## `duckstring secret` — credential store

A **write-only**, catchment-wide store for the credentials a Spout references as `${secret:NAME}`. An alternative to `${env:NAME}` when you'd rather not manage the Catchment's process environment. Secrets are stored at the catchment root (private file, `0600`), **never returned** by the API or CLI (you can list names, not values), and **excluded** from a [`catchment download`](#duckstring-catchment) bundle. Managing secrets requires **full access**.

| Command | Description |
|---|---|
| `secret set {name}` | Store (or overwrite) a secret. The value is **prompted and hidden** — it never appears in your shell history or process arguments. Name must match `[A-Za-z_][A-Za-z0-9_]*`. |
| `secret ls` | List secret names (and when each was set) — never the values. |
| `secret rm {name}` | Remove a secret. |

The value **is** sent to the Catchment over the wire when you set it (an HTTPS POST body) — use TLS, or set it via the server's environment with `${env:NAME}` instead. Encryption-at-rest is not applied: the store is a private plaintext file, secured by filesystem permissions. The same names appear as a picker in the web UI's Spout add form (under the 🔑 menu beside the catchment name).

## `duckstring alert` — notification channels

Deliver failures and staleness to the channels a team already watches. A **channel** is operational config (persisted, survives redeploys), not declared in `pond.toml`. It fires on the events you subscribe it to — `failure` (a Pond Run gave up), `contract` (a breaking schema change), `spout` (an egress delivery failed), `recovery` (a failed Pond/Spout cleared), and `freshness` (a Pond stayed stale past an SLA) — and **root-cause dedup** means one failed Source that blocks twenty downstream Ponds pages you once (about the root, with the blocked names as blast radius), not twenty times. Credentials in the destination URI are `${env:NAME}`/`${secret:NAME}` references, resolved only at send time. Managing channels requires **full access**.

Two destinations work today: a **webhook** (`https://…`/`http://…`, a Slack-incoming-webhook-compatible JSON POST — also any generic receiver) and **email** (`mailto:you@example.com?smtp=host:587&from=alerts@example.com`; SMTP settings from the URI query or the `DUCKSTRING_SMTP_*` environment).

| Command | Description |
|---|---|
| `alert add --to {uri} [--pond N] [--on failure,…\|all] [--stale 1h] [--name N]` | Add a channel. `--to` is an `https://`/`http://`/`mailto:` URI; `--pond` scopes it to one Pond (default: catchment-wide); `--on` is the event kinds (default `all`); `--stale` sets a freshness SLA (e.g. `1h`, `30m`) — required for `freshness` to fire; `--name` defaults to the scheme/scope. |
| `alert ls` | List channels with their scope, events, SLA, and destination. |
| `alert rm {name}` | Remove a channel. |
| `alert test {name}` | Send a test notification through the channel (validates connectivity + credentials). |
| `alert log [--limit N]` | Recent deliveries (channel, kind, pond, status, error) — the audit trail. |

**Freshness is the headline.** A pipeline can be green with zero failures and still be *wrong* because nothing has refreshed it — a `--stale` channel is how you find out. A delivery failure never affects a Pond: it is retried and, if a channel stays broken, parked as `failed` in `alert log`, never cascaded. Channels are also managed from the web UI — a catchment-wide **Alerts** menu (beside 🔑 Secrets) and a per-Pond **Alerts** section in the sidebar. See also the Prometheus [`/metrics`](../guides/running-a-catchment.md#monitoring) endpoint.

## `duckstring control` — execution & health

See [Control](../guides/control.md) and [Fault Tolerance](../guides/fault-tolerance.md).

| Command | Description |
|---|---|
| `control wake {pond}` | Run once when Sources hold fresher data (waits for it; no upstream solicit). Clears failed/killed. |
| `control force {pond}` | Recompute now at current freshness; doesn't propagate downstream. Clears failed/killed. |
| `control refresh {pond} [--clear]` | Flag the Pond so its *next* run is a cold wipe-and-rebuild (full recompute, clears the changelog so downstream reloads). Lazy — nothing runs now. `--clear` un-flags. See [Trickle](../guides/trickle.md). |
| `control repair {ponds}... [--downstream]` | Force-rebuild a **connected** set of Ponds now, in dependency order (each reads its freshly-rebuilt parents). For an immediate fix when no new upstream run is coming. `--downstream` extends the set to all descendants; a disconnected set (a skipped Pond in a sequence) is rejected. |
| `control sleep {pond} [--upstream]` | Clear all demand (started runs complete). `--upstream` also sleeps every ancestor. |
| `control kill {pond}` | Terminate the Pond's worker and cancel its run; parks the Pond `killed` until wake/force/clear. |
| `control clear {pond}` | Reset a failed/killed Pond to idle and unblock downstream, without running. |
| `control failure-budget {pond} [-i N] [-o N]` | Show (no flags) or set the retry budgets: `--immediate` Ripple retries per run, `--on-change` Pond Runs retried as Sources update. |

## `duckstring status` — live monitor

```bash
duckstring status [pond] [-c NAME] [--once]
```

Live view of deployed Ponds: state, freshness, staleness, and standing triggers — open until `Ctrl+C`. With a `pond` argument, shows only that Pond and its upstream lineage. `--once` prints a snapshot and exits; `-m`/`-v` narrow a named Pond to one major line.

## `duckstring get` / `query` — data access

See [Querying Data](../guides/querying-data.md).

```bash
duckstring get {pond} {ripple} [--path DIR]
```

Download a Ripple's published output (default destination `./ponds/{pond}/{ripple}/`).

```bash
duckstring query {pond} [ripple] [--sql SQL | --sql @file.sql]
                 [--csv F | --json F | --parquet F] [--path DIR]
```

Run SQL against the Pond's exported tables. With just a `ripple` argument: `SELECT * FROM {pond}.{ripple} LIMIT 10`. Without a format flag, results print to the terminal; with one, they're written to `./ponds/{pond}/[{ripple}/]{filename}` or `--path`.
