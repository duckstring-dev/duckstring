---
title: Migrating a Project
description: Bring existing SQL and Python transformations into Duckstring.
---

# Migrating a Project

Most projects arrive as a folder of SQL files, a set of Python scripts, or a dbt project, run in order by cron or another scheduler. This guide moves one into Duckstring in two stages: first as a single Pond, unchanged apart from the wrapping, then split into Ponds along ownership lines once it's running.

For a dbt project, see [dbt Projects](dbt_projects.md) instead; the second stage below still applies.

## Stage 1: one Pond

Put the whole project in one Pond, with one Ripple per step. The SQL stays as it is. You gain versioning, dependency ordering, retries, run history and a catalog without redesigning anything.

Suppose the project is a folder of SQL files that build on each other:

```text
analytics/
├── sql/
│   ├── stg_orders.sql
│   ├── stg_customers.sql
│   └── customer_revenue.sql      # reads stg_orders and stg_customers
└── run.sh                        # runs them in order
```

Scaffold a Pond in the project and move the SQL under `src/`:

```bash
cd analytics
duckstring pond init analytics
mkdir -p src/sql && mv sql/*.sql src/sql/
```

Everything in the project directory is uploaded when you deploy, apart from what `.pondignore` excludes (by default local test data, `.env` files, hidden directories and caches), so files next to `src/pond.py` are available at run time. `duckstring pond deploy --dry-run` lists exactly what would be uploaded. A small helper turns each file into a Ripple:

```python
from pathlib import Path

from duckstring import ripple

SQL = Path(__file__).parent / "sql"


def sql_ripple(name, parents=()):
    """A Ripple that writes the table `name` from `sql/{name}.sql`."""
    query = (SQL / f"{name}.sql").read_text()

    def run(pond):
        pond.write_table(name, pond.con.sql(query))

    return ripple(name=name, parents=list(parents))(run)


stg_orders = sql_ripple("stg_orders")
stg_customers = sql_ripple("stg_customers")
customer_revenue = sql_ripple("customer_revenue", parents=[stg_orders, stg_customers])
```

Each SQL file refers to earlier tables by name, exactly as before, because they're all in the Pond's own database. The order that `run.sh` enforced is now the `parents` lists, and independent steps run in parallel.

### Getting data in

If the old project read its raw data from a warehouse or files, those reads become Ripples too. DuckDB reaches most sources directly:

```python
@ripple
def raw_orders(pond):
    pond.con.execute("INSTALL postgres; LOAD postgres")
    pond.con.execute("ATTACH 'postgresql://reader@db.internal/shop' AS shop (TYPE postgres, READ_ONLY)")
    pond.write_table("raw_orders", pond.con.sql("SELECT * FROM shop.public.orders"))
    pond.con.execute("DETACH shop")
```

Read connection details from environment variables on the Catchment rather than writing them into the code.

### Python dependencies

Ripples run in the Catchment's Python environment, so any package a Ripple imports (`requests`, `scikit-learn`, a database driver) must be installed wherever the Catchment runs, and in the image used for [cloud compute](cloud_compute_on_aws.md).

### Replacing the scheduler

Deploy, then replace the cron entry with a trigger on the final table's Pond. A nightly job becomes a Tide:

```bash
duckstring pond deploy
duckstring trigger tide analytics 1d
```

See [Scheduling](scheduling.md) for the other options.

## Stage 2: splitting into Ponds

A single Pond works, but it keeps one version number, one owner and one deployment for everything. Split it when those start to hurt. Good places to cut are:

- **Ownership.** Different people or teams maintain different parts. Each team owns its Ponds and releases them on its own schedule.
- **Reuse.** Several consumers need the same intermediate tables. Make those a Pond they can all pin to.
- **Rate of change.** Staging logic that rarely changes shouldn't be redeployed with a report that changes weekly.
- **Consumers.** Each distinct data product, such as a dashboard's tables or a finance export, is a natural Outlet.

There's no automatic split; it's a judgement about who owns what. The demo pipeline shows the result: `transactions` and `products` are Inlets owned by whoever runs those systems, `sales` combines them, and `reports` is an Outlet for one audience.

### Moving Ripples out

Take the staging steps above into their own Pond. Create it alongside, move the Ripples and SQL across, and give it a first release:

```toml
# staging/pond.toml
[pond]
name = "staging"
version = "1.0.0"
type = "inlet"
```

In the original Pond, declare the new Source and read its tables through `read_table`, which registers them under their own names so the SQL still works:

```toml
# analytics/pond.toml
[pond]
name = "analytics"
version = "2.0.0"

[sources]
staging = "1.0.0"
```

```python
@ripple
def customer_revenue(pond):
    pond.read_table("staging.stg_orders")
    pond.read_table("staging.stg_customers")
    pond.write_table("customer_revenue", pond.con.sql((SQL / "customer_revenue.sql").read_text()))
```

`analytics` no longer publishes `stg_orders` or `stg_customers`, which is a breaking change for anything that read them there, so it moves to a new major version. Deploy `staging` first, then `analytics`:

```bash
(cd staging && duckstring pond deploy)
(cd analytics && duckstring pond deploy)
```

Version 1 of `analytics` keeps running for anything still pinned to it, and version 2 runs alongside. Once nothing depends on version 1, retire it with `duckstring pond remove analytics --major 1`. See [Upgrades and Breaking Changes](upgrades.md) for the details.

Move the trigger to whichever Ponds are now the Outlets. Triggers are set per major version, so set it on version 2:

```bash
duckstring trigger tide analytics 1d --major 2
```
