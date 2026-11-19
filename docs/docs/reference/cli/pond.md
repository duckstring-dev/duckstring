---
title: duckstring pond
description: Scaffold, test locally, deploy, promote and remove Ponds.
sidebar_label: pond
---

# duckstring pond

Commands for Pond projects. `init`, `hydrate`, `run`, `deploy` and `pond puddle` act on the Pond project in the current directory; `remove` and `promote` act on a deployed Pond.

## `init`

```bash
duckstring pond init NAME
```

Scaffolds a Pond project named `NAME` in the current directory: `pond.toml`, `src/pond.py`, `src/puddles.py`, `.gitignore`, `.pondignore` and `README.md`. Fails if the directory already has a `pond.toml`.

## `demo`

```bash
duckstring pond demo [--ripple | --trickle | --tpcds | --gharchive | --dbt | --sql] [--yes]
```

Creates a set of demo Pond projects as subdirectories of the current directory.

| Option | Creates |
|---|---|
| `--ripple` (default) | `transactions`, `products` → `sales` → `reports`: plain Ripples with fixed sleeps, for seeing orchestration at work. |
| `--trickle` | `orders`, `catalog` → `priced` → `revenue`: append and merge Trickles and a builder join, over generated data. |
| `--tpcds` | Six `tpcds_*` Ponds over TPC-DS data generated locally. |
| `--gharchive` | Six `gh_*` Ponds over the public GitHub event archive, fetched over HTTP. |
| `--dbt` | `shop_orders` and `shop_analytics`, a dbt project deployed as a Pond. Needs the `duckstring[dbt]` extra to deploy and run. |
| `--sql` | The `--ripple` set with `sales` and `reports` written as [SQL Ripples](../../guides/sql_ripples.md), including a static table. |
| `--yes`, `-y` | Skip the confirmation. |

## `hydrate`

```bash
duckstring pond hydrate [--source NAME]... [-c NAME] [--set NAME] [--live] [--as-at TIME] [--from-catchment]
```

Sets the Pond's baseline for local runs. It runs the Pond's [`@puddle`](../python/decorators.md#puddle) definitions and writes their output to `puddles/ponds/{source}/data/`, ready for `pond run`, and records what it did in `puddles/hydrated.json`. Hydrating again replaces the baseline, and the next `pond run` starts from it.

Each Source uses the definitions of its [Puddle set](../python/decorators.md#puddle): the one `--set` names, else the one `puddles.toml` gives it, else the default set.

A Source with no definition is skipped with a warning, unless `--live`, `--as-at` or `--from-catchment` reads it from the Catchment.

| Option | Description |
|---|---|
| `--source`, `-s` | Only hydrate these Sources. Repeatable. The others keep their baseline. |
| `--catchment`, `-c` | The Catchment the Sources are read from. Defaults to the default Catchment. |
| `--set` | Use this Puddle set for every Source. |
| `--live` | Read the Sources in place on the Catchment, without copying them. |
| `--as-at` | Hydrate as of this time (ISO, UTC unless it says otherwise). Without `--live`, each Source's state at that time is copied with every change since. |
| `--from-catchment` | Fill Sources with no Puddle definition by downloading their current tables from the Catchment over HTTP. |

A Source read in place is read from a registered local Catchment's root, or from the Catchment's data root with this machine's own credentials, at the major line its `[sources]` pin selects. A Catchment whose data this machine can't reach (a hosted Catchment that keeps its data on its own disk, or a Volume reached through the Files API) can't be read in place. Use `p.catchment()` or `--from-catchment` for those.

**Live Puddles.** With `--live`, a Source with no definition is read in place, and so is a definition that reads its Source with [`p.read_source`](../python/puddle.md#read_source), which is evaluated again by each incremental run. A definition that doesn't read its Source is copied as written. The hydration time (or `--as-at`) is recorded, and the baseline reads each live Source as it stood then, so it stays close to stable without a copy. Nothing on the Catchment changes because a local run reads it: when the Catchment has since dropped a plain table's version from then, the oldest version it keeps is read, with a warning. When the baseline must not change, copy instead.

**History.** With `--as-at` and no `--live`, a Source with no definition is copied with its state at that time and every change since. A definition that reads its Source is run at that time and at each later change, so the copy holds only what the definition keeps. Either way, incremental runs can then step through the Source's real history. A time earlier than a Source keeps (before a merge table's oldest base, or below an append table's floor) is refused, naming the earliest allowed.

For `--as-at` and `--live`, each Source's run history is read from the Catchment, so a Source that runs without changing still counts as fresh. Without it, the Source's change points stand in, with a warning.

Like `run`, `hydrate` runs in the Pond's own environment when it has one.

## `run`

```bash
duckstring pond run [--ripple NAME] [--fresh]
duckstring pond run --incremental [--next-change | --next-run | --most-recent | --to TIME]
```

Runs the Pond once on this machine against its hydrated Puddles, with no Catchment or Duck. Ripples run one at a time in dependency order, and output is written to `puddles/out/`. Inspect it with [`duckstring pond puddle`](#puddle). A Pond with Sources must be hydrated first.

Without `--incremental`, it is a baseline run. It starts from an empty `puddles/out/` and from the Puddles as hydrated, dropping anything steps have written since, and runs with `pond.previous_f` unset. If `puddles/ponds/{this pond}/` exists (a Puddle of the Pond's own output), it is copied in first as the starting state, so incremental Ripples behave as they would on a later run. The run's freshness, `pond.f`, is the oldest of its Sources' freshnesses at the baseline (the time of hydration for generated Puddles).

With `--incremental`, the run goes on from the last one, keeping its output, as a Refresh would run it: every Source must reach a target freshness first, and `pond.previous_f` is the last run's freshness, so Ripples read the changes in between.

| Target | The freshness asked for |
|---|---|
| `--next-change` | The next time any Source changed after the last run. The default. |
| `--next-run` | The Pond's own next run in production. Needs a hydration with `--as-at` or `--live` while the Catchment it's deployed on was reachable. |
| `--most-recent` | The oldest of the Sources' latest freshnesses. |
| `--to` | This time (ISO). |

Each Source is read as it stood when the last of them reached the target, and the run's freshness is the oldest of theirs, as on a Catchment. A Source whose history doesn't reach the target goes on through its [`@puddle.step`](../python/decorators.md#puddlestep), stamped at the target, or at the current time once no Source has history left. A Source with neither, or a live Source the Catchment hasn't brought that far, stops the run with a message naming it.

When the Pond has a `pyproject.toml` and a `.venv` (created by `uv sync`), the command reruns itself with the `.venv`'s Python, so the run uses the Pond's own [dependencies](../pond_toml.md#python-dependencies). That environment must include Duckstring (`uv add duckstring`).

| Option | Description |
|---|---|
| `--ripple`, `-r` | Run only this Ripple, against the existing local output, in the last run's window. |
| `--fresh` | Ignore the Pond's own Puddle and start from nothing. |
| `--incremental`, `-i` | Go on from the last run instead of the baseline. |
| `--next-change`, `--next-run`, `--most-recent`, `--to` | With `--incremental`: the target, as above. |

### `puddles.toml`

An optional file beside `pond.toml` that picks a Puddle set per Source for local runs. It isn't part of the Pond's version, so it can change freely and can be left out of version control.

```toml
[local]
transactions = "recent"   # Source = Puddle set
```

A Source it doesn't list uses the default set. `pond hydrate --set` overrides it for every Source.

## `deploy`

```bash
duckstring pond deploy [-c NAME] [--all] [--git REF] [--dry-run] [--force] [--yes] [--wait [--timeout D]]
```

Packages the Pond project and deploys it to a Catchment. Before deploying, it reports whether the version is new or previously removed (and will be restored), and asks for confirmation. It then prints how many files it's uploading and their total size.

Deploying a version selects it for its major line, replacing whichever version was running there. A new major version is deployed alongside the existing ones. Redeploying the version a line is running is refused: bump the version in `pond.toml`. An [App](../pond_toml.md#app) is the exception, since nothing depends on its version: its running process is stopped, and the next request starts the new code. The Catchment rejects a deployment that breaks a `[sources]` pin; see [`pond.toml`](../pond_toml.md#sources).

The Catchment builds the Pond's [Python environment](../pond_toml.md#python-dependencies) if it declares one, then loads the Pond's code in it to find its Ripples. A deployment fails, and leaves any running copy of that version in place, if the environment can't be built or the code can't be imported. The error includes uv's output or the import traceback. A first deploy of a new environment can take a minute or more.

| Option | Description |
|---|---|
| `--catchment`, `-c` | Catchment to deploy to. |
| `--all` | Deploy every Pond project found in subdirectories of the current directory. |
| `--git` | Deploy a branch, commit or tag instead of the working directory. The Catchment clones the repository from the project's `origin` remote, so it needs access to it. |
| `--dry-run` | List the files a deploy would upload, with their sizes, and upload nothing. Needs no Catchment. |
| `--force` | Replace the version a line is running, in place. A running Duck keeps the code it loaded until it next starts, so changes can take effect partway through. |
| `--yes`, `-y` | Skip confirmations. |
| `--wait` | After deploying, wait until the new version has completed a Run, then exit with its outcome ([Following a request](index.md#following-a-request)). A deploy doesn't start a Run itself, so something has to ask for one. |
| `--timeout` | With `--wait`: give up after this [duration](../formats.md#durations). |

### `.pondignore`

Files matching the patterns in `.pondignore`, at the Pond's root, aren't deployed. The syntax is the same as `.gitignore`, including `!` to re-include a file. Without a `.pondignore`, these defaults apply:

```text
puddles/          # local test data
puddles.toml      # the Puddle sets chosen for it
.env
.env.*            # secrets and local environment
.*/               # hidden directories: .git/, .venv/, tool caches
__pycache__/
*.py[co]
*.egg-info/
dist/
build/
node_modules/
```

A `.pondignore` replaces the defaults entirely, so keep the lines you still want. `pond init` writes one containing them. The same rules apply to a `--git` deploy, where the Catchment also removes the repository's `.git` directory from its copy.

## `remove`

```bash
duckstring pond remove NAME [-c NAME] [-m N] [--wipe] [--yes]
```

Retires a deployed major line: deletes its data, live state and working files, and its Spouts and alert channels. The deployment record and run history are kept, and redeploying the Pond restores the line. Ponds downstream that read it are blocked until it's restored or they stop depending on it.

The line must be idle with no demand; run [`control sleep`](control.md#sleep) first.

| Option | Description |
|---|---|
| `--major`, `-m` | The major line to remove. Defaults to the highest deployed. |
| `--wipe` | Also delete the deployment record, run history and deployed code, as if it had never been deployed. A redeploy starts from scratch. |
| `--yes`, `-y` | Skip the confirmation. |

## `promote`

```bash
duckstring pond promote POND --major N [-c NAME]
```

Makes major version `N` the one the Pond's bare name refers to in catalog SQL, switching every consumer of the bare name at once. Refused if that major doesn't publish every table currently served. See [Upgrades and Breaking Changes](../../guides/upgrades.md#queries-by-name).

## `puddle`

Commands for inspecting a Pond project's local data: the Puddles written by [`pond hydrate`](#hydrate) and the output of [`pond run`](#run). They run against the `puddles/` directory of the current project, in an in-memory DuckDB connection, and need no Catchment.

Tables are available as `"{pond}"."{table}"`, or by bare name when unambiguous. Where the run output has a table with the same name as one in the Pond's own Puddle, the output is used.

### `puddle ls`

```bash
duckstring pond puddle ls
```

Lists each hydrated Source (live, copied, or copied with history, its set, the freshness it was hydrated at and the one the last run read it at), then the Puddle and run output tables, with row counts, sizes and ages.

### `puddle show`

```bash
duckstring pond puddle show REF [--limit N]
```

Prints the first rows of a table.

| Argument / option | Default | Description |
|---|---|---|
| `REF` | | `{pond}.{table}`, or a bare table name. |
| `--limit`, `-n` | `10` | Rows to show. |

### `puddle query`

```bash
duckstring pond puddle query SQL
```

Runs SQL over every Puddle and output table and prints the result.

```bash
duckstring pond puddle query 'SELECT category, SUM(revenue) FROM "sales"."sale_line" GROUP BY 1'
```
