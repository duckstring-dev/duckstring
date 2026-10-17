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
duckstring pond hydrate [--source NAME]... [-c NAME] [--from-catchment]
```

Runs the Pond's [`@puddle`](../python/decorators.md#puddle) definitions and writes their output to `puddles/ponds/{source}/data/`, ready for `pond run`. A Source with no definition is skipped with a warning.

| Option | Description |
|---|---|
| `--source`, `-s` | Only hydrate these Sources. Repeatable. |
| `--catchment`, `-c` | The Catchment used by Puddles that call `p.catchment()`, and by `--from-catchment`. |
| `--from-catchment` | Fill Sources with no Puddle definition by downloading their tables from the Catchment. |

Like `run`, `hydrate` runs in the Pond's own environment when it has one.

## `run`

```bash
duckstring pond run [--ripple NAME] [--fresh]
```

Runs the Pond once on this machine against its hydrated Puddles, with no Catchment or Duck. Ripples run one at a time in dependency order, and output is written to `puddles/out/`. Inspect it with [`duckstring pond puddle`](#puddle).

A full run starts from an empty `puddles/out/`. If `puddles/ponds/{this pond}/` exists (a Puddle of the Pond's own output), it is copied in first as the starting state, so incremental Ripples behave as they would on a later run.

When the Pond has a `pyproject.toml` and a `.venv` (created by `uv sync`), the command reruns itself with the `.venv`'s Python, so the run uses the Pond's own [dependencies](../pond_toml.md#python-dependencies). That environment must include Duckstring (`uv add duckstring`).

| Option | Description |
|---|---|
| `--ripple`, `-r` | Run only this Ripple, against the existing local output. |
| `--fresh` | Ignore the Pond's own Puddle and start from nothing. |

## `deploy`

```bash
duckstring pond deploy [-c NAME] [--all] [--git REF] [--dry-run] [--yes]
```

Packages the Pond project and deploys it to a Catchment. Before deploying, it reports whether the version is new, already deployed (and will be overwritten), or previously removed (and will be restored), and asks for confirmation. It then prints how many files it's uploading and their total size.

Deploying a version selects it for its major line, replacing whichever version was running there. A new major version is deployed alongside the existing ones. The Catchment rejects a deployment that breaks a `[sources]` pin; see [`pond.toml`](../pond_toml.md#sources).

The Catchment builds the Pond's [Python environment](../pond_toml.md#python-dependencies) if it declares one, then loads the Pond's code in it to find its Ripples. A deployment fails, and leaves any running copy of that version in place, if the environment can't be built or the code can't be imported. The error includes uv's output or the import traceback. A first deploy of a new environment can take a minute or more.

| Option | Description |
|---|---|
| `--catchment`, `-c` | Catchment to deploy to. |
| `--all` | Deploy every Pond project found in subdirectories of the current directory. |
| `--git` | Deploy a branch, commit or tag instead of the working directory. The Catchment clones the repository from the project's `origin` remote, so it needs access to it. |
| `--dry-run` | List the files a deploy would upload, with their sizes, and upload nothing. Needs no Catchment. |
| `--yes`, `-y` | Skip confirmations. |

### `.pondignore`

Files matching the patterns in `.pondignore`, at the Pond's root, aren't deployed. The syntax is the same as `.gitignore`, including `!` to re-include a file. Without a `.pondignore`, these defaults apply:

```text
puddles/          # local test data
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

Lists hydrated Puddles and run output tables, with row counts, sizes and ages.

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
