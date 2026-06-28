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

Publish a Pond's output to external systems. A Spout is operational config (persisted, survives redeploys), not declared in `pond.toml`. Credentials go in the destination URI as `${env:NAME}` references, resolved only at egress time. After each successful Pond Run, the egress worker delivers the Pond's published tables to the destination as snapshot Parquet (`{prefix}/{table}.parquet`).

`file://`, `s3://`, and `gs://` work today. Object-store credentials go in the URI query: `s3://bucket/prefix?key_id=${env:AWS_KEY}&secret=${env:AWS_SECRET}&region=us-east-1` (also `endpoint`, `url_style`, `use_ssl`, `session_token`); `s3://` with no key falls back to the AWS credential chain (env / instance profile); `gs://` needs HMAC `key_id`+`secret`. *(The incremental Postgres sink is landing — a destination whose driver isn't built yet parks the Spout with a clear error.)*

| Command | Description |
|---|---|
| `spout add {pond} --to {uri} [--table T \| --all] [--mode auto\|full\|append] [--name N]` | Bind a Spout. `--to` is a `file://`/`s3://`/`gs://`/`postgres://` URI (credentials as `${env:NAME}`); `--table` egresses one table, default all; `--mode` defaults `auto`; `--name` defaults to the table (or scheme), `-2`/`-3` on collision. |
| `spout ls {pond}` | List the Pond's Spouts with their delivery watermark and state (ok / retrying / failed). |
| `spout rm {pond} {name}` | Remove a Spout. |
| `spout resync {pond} {name}` | Force a full re-egress (clears the watermark + any failure). |

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
