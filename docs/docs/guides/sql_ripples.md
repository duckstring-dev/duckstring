---
title: SQL Ripples
description: Write Ripples as SQL files declared in pond.toml.
---

# SQL Ripples

A Ripple can be a single SQL query in a file instead of a Python function. Each one is declared in `pond.toml` with the Ripples it depends on, the Source tables it reads and how it writes its table, so a pipeline of SQL needs no Python. SQL and Python Ripples can be mixed in one Pond.

Use SQL Ripples for pipelines written as plain queries, including SQL brought over from another scheduler. Use a [dbt project](dbt_projects.md) when you want to keep dbt's macros, tests and documentation as they are.

The worked example is the `sales` Pond from `duckstring pond demo --sql`, which creates the demo pipeline with `sales` and `reports` written in SQL.

## Declaring Ripples

```text
sales/
├── pond.toml
├── data/
│   └── tier_bands.csv
└── sql/
    ├── daily_sales.sql
    ├── price_tiers.sql
    └── sale_line.sql
```

```toml
[pond]
name = "sales"
version = "1.0.0"

[sources]
transactions = "1.0.0"
products = "1.0.0"

[ripples.daily_sales]
sql = "sql/daily_sales.sql"
reads = ["transactions.transaction"]

[ripples.price_tiers]
sql = "sql/price_tiers.sql"
reads = ["products.product"]

[ripples.sale_line]
sql = "sql/sale_line.sql"
parents = ["daily_sales", "price_tiers"]

[static.tier_bands]
path = "data/tier_bands.csv"
```

Each `[ripples.NAME]` entry is one Ripple. Its file holds one `SELECT`, and the result is written to a table with the Ripple's name, so `sale_line` writes the table `sale_line`. `parents` lists the Ripples that must finish first, and Ripples with no parents in common run in parallel. Here `daily_sales` and `price_tiers` run side by side, and `sale_line` runs once both have finished.

```sql
-- sql/sale_line.sql
SELECT
    s.sale_date,
    s.product_id,
    p.name AS product_name,
    p.category,
    p.price_tier,
    s.total_quantity,
    p.unit_price,
    ROUND(s.total_quantity * p.unit_price, 2) AS revenue,
    s.tx_count
FROM daily_sales AS s
LEFT JOIN price_tiers AS p ON s.product_id = p.product_id
```

The Ripples in a Pond share one DuckDB database, so a query refers to its parents' tables by name.

## Reading other Ponds

A Source table is listed in `reads` and written in the query as `source.table`, the same name the catalog uses:

```sql
-- sql/daily_sales.sql
SELECT
    product_id,
    created_at AS sale_date,
    SUM(quantity) AS total_quantity,
    COUNT(*) AS tx_count
FROM transactions.transaction
WHERE product_id IS NOT NULL AND quantity > 0
GROUP BY product_id, created_at
```

Every Source in `reads` must also be in `[sources]`. As with Python Ripples, the query reads the version of each Source table that was published when the Pond Run started, even if the Source publishes again while the run is in progress.

## Checked references

Nothing about a SQL Ripple is worked out from its SQL: the declaration in `pond.toml` decides the order. The SQL is checked against it instead. When the Pond is deployed, and on every local run, each query is parsed and every table it reads must be one of:

- a Ripple among its `parents`, or further up that chain;
- a Source table in its `reads`;
- a static table (see below);
- the Ripple's own table, which holds its output from the previous run.

Anything else rejects the deployment with a message naming the Ripple and the table. A forgotten `parents` entry is the case this catches: without it, the Ripple could run before its parent and silently read the table that parent wrote last time.

The query's own CTEs, and table functions such as `read_parquet(...)`, aren't tables in this sense and need no declaring. A query that reads tables a Python parent writes can't be checked at deployment, since a Python Ripple's tables aren't known until it runs. Those reads are allowed when a Python Ripple is among its parents (directly or further up).

## Writing modes

A SQL Ripple replaces its table on every run by default. `write` makes it a [Trickle](append_and_merge.md) instead:

```toml
[ripples.daily_sales]
sql = "sql/daily_sales.sql"
reads = ["transactions.transaction"]
write = "merge"
pk = ["product_id", "sale_date"]
```

| `write` | Behaves like | `pk` |
|---|---|---|
| `overwrite` (default) | `pond.write_table` | not allowed |
| `merge` | `pond.merge_table`: the query is the table's complete current state, and the changes are recorded | required |
| `append` | `pond.append_table`: each run's rows are added to the history | optional |

The query itself still runs in full each run. For joins and aggregations maintained incrementally, write a Python Ripple with the [Trickle builder](joins_with_the_builder.md).

## Static tables

A file shipped with the Pond's code, such as a lookup table kept in the repository, is declared under `[static.NAME]`:

```toml
[static.tier_bands]
path = "data/tier_bands.csv"
```

Every Ripple, SQL or Python, sees it as a read-only table with that name:

```sql
-- sql/price_tiers.sql
SELECT p.id AS product_id, p.name, p.category, p.unit_price, b.price_tier
FROM products.product AS p
JOIN tier_bands AS b ON p.unit_price >= b.min_price AND p.unit_price < b.max_price
```

The path is relative to the Pond's directory, and the format comes from the extension: CSV, TSV, Parquet or JSON. The file is deployed with the code, so a changed file takes effect from the next run after a redeploy. Static tables aren't published, and they don't trigger a run when they change.

For data that changes independently of the code, such as files landing in a bucket or another database, write an Inlet instead.

## Mixing SQL and Python

A Pond can have SQL Ripples in `pond.toml` and `@ripple` functions in `src/pond.py` together. Either kind can name the other in its parents; on the Python side, `parents` accepts names as well as function references:

```python
from duckstring import ripple

@ripple(parents=["sale_line"])
def forecast(pond):
    history = pond.read_table("sale_line")
    ...
```

Python Ripples get the same protection at run time for the reads that go through the `pond` handle (`read_table`, `count_table` and `pond.trickle(...)` on the Pond's own tables): reading a table written by a Ripple that isn't among its parents fails the Ripple with a message naming both. SQL run directly on `pond.con` isn't checked, so declare those parents carefully.

## Running

SQL Ripples run like any other. `duckstring pond hydrate` and `duckstring pond run` work as described in [Testing with Puddles](testing_with_puddles.md), and `duckstring pond deploy` uploads the SQL and static files with the rest of the project. A declaration error, such as an unknown parent, a missing file or an undeclared table, stops both the local run and the deployment with the same message.
