---
title: dbt Projects
description: Deploy an existing dbt project as a Pond.
---

# dbt Projects

A dbt project can be deployed as a Pond without writing any Ripples. Each dbt model becomes a Ripple, and `ref()` sets the order they run in, so the project gains versioning, pull-based scheduling, retries and a catalog while the models stay as they are. dbt Ponds and Python Ponds can depend on each other freely.

The worked example below is `shop_analytics`, created with `duckstring pond demo --dbt` alongside the Python Pond it reads from, `shop_orders`.

## Install the extra

```bash
pip install 'duckstring[dbt]'
```

This installs dbt-core and the dbt-duckdb adapter. Install it wherever the Catchment runs, since Ducks run the models there. Alternatively, give the dbt Pond its own [environment](writing_ripples.md#python-dependencies) with `uv add 'duckstring[dbt]'`, which also lets two dbt Ponds use different dbt versions.

## Set up the Pond

Put the dbt project inside a Pond directory and point `pond.toml` at it:

```text
shop_analytics/
├── pond.toml
└── dbt/
    ├── dbt_project.yml
    └── models/
        ├── sources.yml
        ├── orders_clean.sql
        ├── revenue_by_product.sql
        └── top_products.sql
```

```toml
[pond]
name = "shop_analytics"
version = "1.0.0"
type = "outlet"
dbt_project = "dbt"

[sources]
shop_orders = "1.0.0"
```

A dbt Pond has no `src/pond.py`; its Ripples are its models.

Three settings in `dbt_project.yml` matter:

```yaml
name: 'shop_analytics'
profile: 'duckstring'
model-paths: ["models"]

models:
  shop_analytics:
    +materialized: table
```

- `profile` must be `duckstring`. Duckstring generates the `profiles.yml` for this profile, pointing dbt at the Pond's own DuckDB database. Don't supply your own.
- Materialise models as tables. Only tables are published; a model materialised as a view is visible to later models in the Pond but not to other Ponds or the catalog.
- Build models in the default schema. Only tables in the `main` schema are published, so a model with a custom `schema` config isn't.

## Reading other Ponds

A dbt `source()` whose source name matches a Pond in `[sources]` reads that Pond's published table. Declare it in the usual `sources.yml`:

```yaml
version: 2

sources:
  - name: shop_orders
    schema: shop_orders
    tables:
      - name: sale
```

```sql
-- models/orders_clean.sql
select sale_id, product, amount, sale_date
from {{ source('shop_orders', 'sale') }}
where amount > 0
```

Before a model runs, Duckstring loads each Source table it reads into the Pond's database at the exact relation dbt expects (here `shop_orders.sale`). Keep the source's `schema` as anything other than `main`, or the loaded copy would be republished as part of this Pond's output.

A `source()` whose name isn't a declared Pond is left to dbt, for example a table dbt reads through an attached database of its own.

## What runs

When the Pond runs, each model runs as its own Ripple with `dbt run --select <model>`, in `ref()` order, with independent models in parallel. Each model gets its own run history, error message and retries. A model that still fails after its retries fails the Pond Run, and run history shows which model it was.

Only models become Ripples. Seeds, snapshots and dbt tests aren't run by Duckstring. Duckstring's own checks still apply: a run whose output drops a table or column fails without publishing, as for any Pond (see [Upgrades and breaking changes](upgrades.md)).

dbt incremental models keep dbt's own behaviour, since the Pond's database persists between runs. Their output is published as an ordinary table, so downstream Ponds read it in full rather than as a stream of changes. Use a Python Pond with [Trickles](append_and_merge.md) where downstream incremental reads matter.

A dbt Pond can't also contain `@ripple` functions. Put Python steps in a separate Pond upstream or downstream.

## Deploying and running

```bash
cd shop_orders && duckstring pond deploy && cd ..
cd shop_analytics && duckstring pond deploy && cd ..
duckstring trigger pulse shop_analytics
```

Deployment parses the dbt project to find its models. A parse error, or a missing `dbt_project.yml`, rejects the deployment with dbt's message.

`duckstring pond run` doesn't support dbt Ponds. Develop the models with dbt as usual, then test the Pond against a [local Catchment](../concepts/management_and_execution.md#local-execution).
