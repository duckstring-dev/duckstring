---
title: Writing Ripples
description: Build a Pond's transformations in SQL and Python.
---

# Writing Ripples

This guide walks through writing the Ripples of a Pond: reading Sources, writing tables, splitting work into steps, and handling outputs that aren't tables. It assumes you've read [Ponds](../concepts/ponds.md) and [Ripples](../concepts/ripples.md).

## Starting a Pond

Create an empty directory and scaffold a Pond in it:

```bash
mkdir sales && cd sales
duckstring pond init sales
```

This writes `pond.toml`, `src/pond.py` for the Ripples, `src/puddles.py` for [local test data](testing_with_puddles.md), a `.gitignore`, a `.pondignore` listing files that deploying leaves out, and a `README.md`. Declare the Ponds you read from in `pond.toml`:

```toml
[pond]
name = "sales"
version = "0.1.0"

[sources]
transactions = "1.0.0"
products = "1.0.0"
```

## A first Ripple

A Ripple reads, transforms and writes. Here is `daily_sales` from the demo `sales` Pond:

```python
from duckstring import ripple


@ripple
def daily_sales(pond):
    pond.read_table("transactions.transaction")
    agg = pond.con.sql("""
        SELECT product_id,
               created_at    AS sale_date,
               SUM(quantity) AS total_quantity,
               COUNT(*)      AS tx_count
        FROM "transaction"
        WHERE product_id IS NOT NULL AND quantity > 0
        GROUP BY product_id, created_at
    """)
    pond.write_table("daily_sales", agg)
```

`pond.read_table("transactions.transaction")` makes the Source table available to SQL under its own name, `transaction`. (It's quoted here only because `transaction` is a SQL keyword.) `pond.con` is a DuckDB connection to the Pond's own database, and `pond.write_table` replaces the `daily_sales` table with the query result. Nothing is visible to other Ponds until the whole Pond Run succeeds, at which point every table in the Pond's database is published.

:::tip
In SQL, refer to Source tables by the name `read_table` registers, not by a Python variable holding a relation (`FROM rel`). DuckDB resolves Python variables by scanning the call stack, which is unreliable when Ripples run in parallel.
:::

## Splitting work into Ripples

Give each logical step its own Ripple, and declare what it depends on with `parents`:

```python
@ripple
def price_tiers(pond):
    pond.read_table("products.product")
    pond.write_table("price_tiers", pond.con.sql("""
        SELECT id AS product_id, name, category, unit_price,
               CASE WHEN unit_price < 25  THEN 'budget'
                    WHEN unit_price < 150 THEN 'standard'
                    ELSE 'premium' END AS price_tier
        FROM product
    """))


@ripple(parents=[daily_sales, price_tiers])
def join_lines(pond):
    pond.write_table("sale_line", pond.con.sql("""
        SELECT s.sale_date, s.product_id, p.name AS product_name, p.category, p.price_tier,
               s.total_quantity, p.unit_price,
               ROUND(s.total_quantity * p.unit_price, 2) AS revenue, s.tx_count
        FROM daily_sales s
        LEFT JOIN price_tiers p ON s.product_id = p.product_id
    """))
```

`join_lines` queries `daily_sales` and `price_tiers` directly, because they're this Pond's own tables. `daily_sales` and `price_tiers` don't depend on each other, so they run in parallel.

Splitting has practical benefits beyond readability. A failed Ripple can be retried on its own, run history shows the time each step took, and the UI shows where a run is. As a rule of thumb, split where you'd want to see progress or where steps can run side by side, and keep a Ripple together where its steps only make sense as a whole.

A Ripple can write several tables. They're written in the order the code writes them, one after another; split them into separate Ripples if you want them computed in parallel.

## Working in Python

The connection is a normal DuckDB connection, so anything DuckDB's Python API does works here. You can move between SQL, the relation API and DataFrames:

```python
@ripple
def enrich(pond):
    pond.read_table("sales.sale_line")
    df = pond.con.sql("SELECT * FROM sale_line").df()      # a pandas DataFrame
    df["margin_band"] = df["revenue"].apply(classify)       # any Python logic
    pond.write_table("sale_line_banded", pond.con.from_df(df))
```

`write_table` takes a DuckDB relation, so convert a DataFrame with `pond.con.from_df`, or an Arrow table with `pond.con.from_arrow`.

For scratch work inside a Ripple, use temporary tables. Only tables in the Pond database's `main` schema are published, and each Ripple has its own connection, so a `TEMP` table stays private to the Ripple that created it:

```python
pond.con.execute("CREATE TEMP TABLE recent AS SELECT * FROM sale_line WHERE sale_date > current_date - 30")
```

## Inlets: reading from outside

An Inlet has no Sources, so its Ripples fetch data from somewhere else: an API, a database, files in object storage. DuckDB reads most of these directly:

```python
@ripple
def ingest(pond):
    pond.write_table("exchange_rate", pond.con.sql("""
        SELECT * FROM read_json_auto('https://example.com/rates/latest.json')
    """))
```

When an Inlet should add to what it already has rather than replace it, read its own table first. `pond.f`, the run's freshness, makes a stable stamp: a retried or recovered run reuses the same `f`, so rows it stamps are identical on replay.

```python
@ripple
def ingest(pond):
    new = pond.con.sql(f"""
        SELECT *, TIMESTAMPTZ '{pond.f.isoformat()}' AS ingested_at
        FROM read_parquet('s3://drops/events/*.parquet')
    """)
    pond.append_table("event", new)
```

`append_table` is covered in [Append and merge tables](append_and_merge.md). It keeps history in a form downstream Ponds can read incrementally, and a replay at the same `f` replaces that run's rows instead of duplicating them.

## Outputs that aren't tables

A trained model, a vectoriser or a rendered report can be published as an Object. An Object is a single file or directory, replaced as a whole on each run:

```python
import pickle


@ripple
def train(pond):
    pond.read_table("sales.sale_line")
    model = fit_model(pond.con.sql("SELECT * FROM sale_line").df())
    pond.write_object("demand_model", pickle.dumps(model))


@ripple(parents=[train])
def forecast(pond):
    model = pickle.loads(pond.read_object("demand_model"))
    ...
```

A downstream Pond reads it as `pond.read_object("forecasting.demand_model")`, or gets a local path to a directory Object with `pond.object_path(...)`. See [Pond Handle](../reference/python/pond.md#objects).

Object names may contain dots, such as `demand_model.pkl`, but references split at the first dot, so a dotted name must be read back in backticks: ``pond.read_object("`demand_model.pkl`")``, or ``"forecasting.`demand_model.pkl`"`` from downstream. Plain names are simpler.

## Ripples with side effects

Duckstring skips a Pond Run when none of its Sources have changed since the last one. That's right for a pure transformation, but a Ripple that sends an email or calls a webhook might need to act every run. Declare it with `always_run=True`, and use `sources_changed()` to skip any data work that isn't needed:

```python
@ripple(always_run=True)
def notify(pond):
    post_heartbeat("sales")                  # every run
    if not pond.sources_changed():
        pond.skip()                          # tell downstream nothing changed
        return
    pond.read_table("sales.sale_line")
    post_summary(pond.con.sql("SELECT SUM(revenue) FROM sale_line").fetchone()[0])
```

`pond.skip()` marks the run as producing no change, so Ponds downstream can skip their own runs too. You can call it from any Ripple that detects it had nothing new to write.

Side effects can run more than once. A Ripple is retried after a failure, and runs again after a crash if it hadn't finished, so make external calls safe to repeat, for example by keying them on `pond.f`.

## Python dependencies

When Ripples import packages beyond Duckstring and DuckDB, give the Pond its own environment. In the Pond's directory:

```bash
uv init --bare                 # writes a minimal pyproject.toml
uv add duckstring scikit-learn
```

This writes `pyproject.toml` and `uv.lock` and creates the environment in `.venv`. `duckstring pond run` and `pond hydrate` switch to that environment automatically, so local runs use the same packages a deployed run will. Commit both files.

On deploy, the Catchment builds the environment from `uv.lock`, installs its own version of Duckstring into it, and runs the Pond's Ducks there. Environments are cached by the lock's contents, so redeploying with an unchanged lock is quick, and Ponds with identical locks share one. A deploy is refused if `uv.lock` is missing or out of date with `pyproject.toml`; run `uv lock` and deploy again. To pin a Python version, add a `.python-version` file (`uv python pin 3.12`). Otherwise the Pond uses the Catchment's Python.

A Pond without a `pyproject.toml` runs in the Catchment's own environment.

:::note
Pond environments are built for Ducks on the Catchment's own machine. A Pond whose Duck runs on a pool or in the [cloud](cloud_compute_on_aws.md) still uses the packages installed there, such as those in the Duck image.
:::

## Deploying

From the Pond's directory:

```bash
duckstring pond deploy
```

Test it locally first with [Puddles](testing_with_puddles.md), and see [Upgrades and breaking changes](upgrades.md) before changing a Pond that others depend on.
