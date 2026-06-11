---
title: CLI
description: Full command reference for the duckstring / ds CLI.
---

# CLI Reference

The CLI installs as both `duckstring` and `ds`; the two are identical. Every command and group prints detailed help with `--help`, and shell completions install with `duckstring --install-completion`.

## Common options

Most commands that talk to a Catchment share these:

| Option | Meaning |
|---|---|
| `--catchment`, `-c {name}` | Target a registered Catchment (default: the configured default; if exactly one is registered, it's implicit) |
| `--major`, `-m {int}` | Target a specific major version line (default: latest active) |
| `--version`, `-v {semver}` | Target a specific version, e.g. `1.2.3` |
| `--silent` | Submit without opening the live status view |
| `--watch` | Keep the status view open even after a one-shot settles |

## `duckstring catchment` — work with Catchments

| Command | Description |
|---|---|
| `catchment init -n {name} [--host H] [-p PORT] [--root DIR] [-y]` | Create and register a local Catchment, then start its server. Defaults: host `127.0.0.1`, port `7474`, root `~/.duckstring/{name}`. Offers to set as default (`-y` accepts). |
| `catchment start {name}` | Start the server for a registered local Catchment. |
| `catchment connect -n {name} --path {url} [-y]` | Register a remote Catchment by URL. |
| `catchment list` | List registered Catchments; `●` marks the default. |
| `catchment set-default {name}` | Set the default Catchment. |
| `catchment disconnect {name} [--purge]` | Unregister; for local Catchments, offers to delete the data directory (`--purge` deletes without asking). |

Registrations and the default live in `~/.duckstring/config.toml`.

## `duckstring pond` — manage Pond projects

| Command | Description |
|---|---|
| `pond init {name}` | Scaffold a new Pond project in the current (empty) directory. |
| `pond demo` | Create the four demo Pond projects (`transactions`, `products`, `sales`, `reports`) as subdirectories. |
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

## `duckstring control` — execution & health

See [Control](../guides/control.md) and [Fault Tolerance](../guides/fault-tolerance.md).

| Command | Description |
|---|---|
| `control wake {pond}` | Run once when Sources hold fresher data (waits for it; no upstream solicit). Clears failed/killed. |
| `control force {pond}` | Recompute now at current freshness; doesn't propagate downstream. Clears failed/killed. |
| `control sleep {pond} [--upstream]` | Clear all demand (started runs complete). `--upstream` also sleeps every ancestor. |
| `control kill {pond}` | Terminate the Pond's worker and cancel its run; parks the Pond `killed` until wake/force/clear. |
| `control clear {pond}` | Reset a failed/killed Pond to idle and unblock downstream, without running. |
| `control failure-budget {pond} [-i N] [-o N]` | Show (no flags) or set the retry budgets: `--immediate` Ripple retries per run, `--on-change` Pond Runs retried as Sources update. |

## `duckstring status` — live monitor

```bash
duckstring status [pond] [-c NAME] [--all] [--once] [--watch]
```

Live view of active Ponds: state, freshness, staleness, and standing triggers. With a `pond` argument, shows only that Pond and its upstream lineage. `--once` prints a snapshot and exits; `--watch` never auto-exits; `--all`/`-a` includes inactive Ponds; `-m`/`-v` select a version line.

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
