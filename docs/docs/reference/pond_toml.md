---
title: pond.toml
description: Every section and key.
---

# pond.toml

Every Pond project has a `pond.toml` at its root, declaring what the Pond is and what it depends on. It's read when the Pond is deployed and on every local run. Environment-specific settings, such as triggers, windows, Spouts, secrets and alerts, are configured on the Catchment instead and don't appear here.

```toml
[pond]
name = "sales"
version = "1.2.0"
type = "pond"
immediate_retries = 1
source_retries = 2

[sources]
transactions = "1.0.0"
products = "1.1.0"
```

## `[pond]`

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | The Pond's name, unique within a Catchment. |
| `version` | string | required | A semantic version, `MAJOR.MINOR.PATCH`. The major version decides which line a deployment joins: the same major upgrades the running line, a new major deploys alongside it. See [Versioning](../concepts/ponds.md#versioning). |
| `type` | string | `"pond"` | `"inlet"`, `"pond"` or `"outlet"`. Descriptive: it records the Pond's intended role and is shown in the UI, but doesn't change how it runs. |
| `ripples` | string | `"src/pond.py"` | Path to the module defining the Python Ripples. Optional when the Pond has only [SQL Ripples](#ripples). |
| `puddles` | string | `"src/puddles.py"` | Path to the module defining the Puddles. |
| `immediate_retries` | integer | `0` | How many times a failed Ripple is retried within the same Pond Run. |
| `source_retries` | integer | `0` | How many times a failed Pond Run is retried when a Source next updates. |
| `duck` | string | `"catchment"` | Where the Pond runs: `"catchment"` for the Catchment's own machine, a built-in pool (`"S"`, `"M"`, `"L"`, `"XL"`), or the name of a pool defined on the Catchment. Falls back to the Catchment's machine when cloud compute isn't configured or the pool doesn't exist. See [`duckstring duck`](cli/duck.md). |
| `dbt_project` | string | | Path to a dbt project directory. Makes this a dbt Pond: each model becomes a Ripple, and the Pond can't also define `@ripple` functions, SQL Ripples or static tables. Needs the `duckstring[dbt]` extra. |

The two retry budgets set the starting values when a Pond is first deployed. After that the Catchment's values are authoritative, and are changed with [`duckstring control failure-budget`](cli/control.md#failure-budget).

## `[sources]`

Each key is a Source Pond's name, and each value pins it:

```toml
[sources]
transactions = "1.2.0"     # major version 1, at least 1.2.0
products = "2.0.0?"        # optional: major version 2, at least 2.0.0
```

| Value | Meaning |
|---|---|
| `"MAJOR.MINOR.PATCH"` | A required Source. The Pond reads that major line, and the deployed version must be at least this one. The Pond waits for the Source before running, and its freshness is the oldest of its required Sources. |
| `"MAJOR.MINOR.PATCH?"` | An optional Source. The Pond uses whatever the Source has published but never waits for it. A Pond with only optional Sources takes the freshness of the freshest. |

Deployment enforces the pins in both directions: a Pond can't be deployed against a Source older than its minimum, and a Source can't be redeployed below a version that a deployed Pond requires. Both are rejected with an error.

A Source doesn't need to be deployed first. A Pond whose Source is missing is held until it arrives.

## `[ripples]`

Declares [SQL Ripples](../guides/sql_ripples.md), one table per Ripple. They can be mixed with the Python Ripples in the [`ripples`](#pond) module.

```toml
[ripples.sale_line]
sql = "sql/sale_line.sql"
parents = ["daily_sales", "price_tiers"]
reads = ["products.product"]
write = "merge"
pk = ["sale_date", "product_id"]
```

The entry's name (`sale_line`) is the Ripple's name and the name of the table it writes. Names are letters, digits and underscores, and are shared with Python Ripples and static tables, so each must be unique.

| Key | Type | Default | Description |
|---|---|---|---|
| `sql` | string | required | Path, relative to the Pond's directory, of a file holding one `SELECT`. |
| `parents` | list of strings | `[]` | Ripples in this Pond, SQL or Python, that must finish before this one starts. |
| `reads` | list of strings | `[]` | Tables of other Ponds the query reads, as `source.table`, each from a Source in `[sources]`. The query refers to them by the same name. |
| `write` | string | `"overwrite"` | `"overwrite"` replaces the table each run, `"merge"` records changes to a table whose query returns its complete current state, and `"append"` adds each run's rows to its history. See [Append and Merge Tables](../guides/append_and_merge.md). |
| `pk` | string or list | | Primary key columns. Required for `"merge"`, optional for `"append"`, not allowed for `"overwrite"`. |
| `always_run` | boolean | `false` | Run even when no Source has changed since the last Pond Run, as [`@ripple(always_run=True)`](python/decorators.md#ripple). |

Each query may only read tables its declaration accounts for: an ancestor's table, an entry in `reads`, a static table, or its own previous output. A query reading anything else, an unknown parent, a cycle, or a missing file rejects the deployment and stops a local run, with a message naming the Ripple.

## `[static]`

Declares files shipped with the Pond's code as read-only tables, visible to every Ripple under the table's name.

```toml
[static.tier_bands]
path = "data/tier_bands.csv"
```

| Key | Type | Description |
|---|---|---|
| `path` | string | Path relative to the Pond's directory. CSV (`.csv`, `.tsv`), Parquet (`.parquet`) or JSON (`.json`, `.jsonl`, `.ndjson`), chosen by the extension. |

Static tables aren't published. A changed file takes effect from the first run after it's redeployed, and doesn't start a run by itself. The file must be included in the deployment, so check `.pondignore` doesn't exclude it.

## `[flock]`

Allows over-sized computations to be sent to a serverless engine. See [Ducks and Flocks](../concepts/management_and_execution.md#ducks-and-flocks).

| Key | Type | Default | Description |
|---|---|---|---|
| `mode` | string | `"off"` | `"off"`: never send work out. `"upgrade"`: send a full recompute out only when it is clearly too large for the Duck, or after it runs out of memory locally. `"always"`: send every eligible full recompute out. |
| `engine` | string | `"athena"` | The engine to use. A built-in name, or `module:Class` for a custom engine. |
| `oom_policy` | string | `"fail_up"` | `"fail_up"`: when a local recompute runs out of memory, retry it on the engine. `"fail"`: let it fail. |

The defaults come from the Catchment (`DUCKSTRING_FLOCK_MODE`, `DUCKSTRING_FLOCK_ENGINE`, `DUCKSTRING_FLOCK_OOM_POLICY`) when the key is omitted. The Flock only takes effect when an engine is configured on the Catchment. Otherwise everything runs in the Duck. Operators can override these per Pond with [`duckstring duck set`](cli/duck.md#set).

## Python dependencies

A Pond's Python packages aren't declared in `pond.toml`, but in a standard `pyproject.toml` beside it, locked into a `uv.lock`:

```toml
# pyproject.toml
[project]
name = "sales"
version = "1.2.0"
requires-python = ">=3.10"
dependencies = ["duckstring", "scikit-learn>=1.5"]
```

| Files present | Environment |
|---|---|
| Neither | The Catchment's own Python environment. |
| `pyproject.toml` and `uv.lock` | The Pond's own, built by the Catchment from `uv.lock` on deploy. |
| `pyproject.toml` only | Deploy is refused: run `uv lock`. |

The Catchment builds the environment with `uv sync --locked`, so a `uv.lock` that no longer matches `pyproject.toml` is also refused. Dev dependencies are left out. The Catchment then installs its own version of Duckstring into the environment, replacing any version in the lock, since the Duck and the Catchment must match. The environment uses the Catchment's Python version, unless the Pond has a `.python-version` file.

Lock against a released Duckstring. A lock made against a local checkout (`uv add --editable ../duckstring`) names a path that exists only on your machine, so a Catchment elsewhere refuses it.

Environments are stored under the Catchment's state directory in `envs/`, one per distinct lock, and shared by every Pond with the same one. A Duck on a pool or in the cloud builds the environment on its own machine when it starts, from the lock the Catchment checked, with that machine's Python and Duckstring. A pool machine keeps it for later Ducks; a cloud Duck builds it again on each cold start.

`requirements.txt` isn't read. It has no lock, so the deployed environment could differ from the one the Pond was tested in.

## `[serve]`

| Key | Type | Default | Description |
|---|---|---|---|
| `tables` | list of strings | `[]` | Tables exposed to users with read-only access when querying the catalog. Users with full access see every table. Can be overridden on the Catchment with [`duckstring serve expose`](cli/serve.md#expose). |
