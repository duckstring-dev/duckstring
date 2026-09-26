---
title: Creating a Pond
description: Write your own Pond from scratch.
---

# Creating a Pond

This guide builds a real Pond: scaffold the project, write Ripples, declare Sources, and test the logic locally. It assumes the [Quickstart](../getting-started/quickstart.md)'s demo Ponds are deployed, since the new Pond will consume one of them.

## Scaffold

In an empty directory:

```bash
duckstring pond init top_sellers
```

This creates the standard Pond layout:

```text
top_sellers/
├── src/
│   ├── pond.py      # your Ripples
│   └── puddles.py   # Source snapshots for local testing
├── pond.toml        # identity + Sources
├── .gitignore
└── README.md
```

with a minimal manifest and a single blank Ripple.

## Declare identity and Sources

Edit `pond.toml`:

```toml
[pond]
name = "top_sellers"
version = "0.1.0"
type = "outlet"

[sources]
sales = "1.0.0"
```

Three decisions live here:

- **`type`** — this Pond consumes `sales` and feeds nothing, so it's an `outlet`. Inlets (no Sources) declare `type = "inlet"`; the default is a plain `pond`.
- **`[sources]`** — each entry is a Source Pond's name and the minimum version of the major line to consume. This single section is the Pond's entire contribution to the pipeline graph.
- **`version`** — starts pre-1.0 while the table contract is settling. See [Versioning](../concepts/versioning.md).

The full manifest format, including optional Sources and retry defaults, is in the [pond.toml reference](../reference/pond-toml.md).

## Write the Ripples

Replace `src/pond.py`:

```python
from duckstring import ripple


@ripple
def product_rank(pond):
    pond.read_table("sales.sale_line")    # registers the Source table as the view `sale_line`
    ranked = pond.con.sql("""
        SELECT product_name, category,
               SUM(revenue)        AS total_revenue,
               SUM(total_quantity) AS units_sold,
               RANK() OVER (ORDER BY SUM(revenue) DESC) AS rank
        FROM sale_line
        GROUP BY product_name, category
    """)
    pond.write_table("product_rank", ranked)


@ripple(parents=[product_rank])
def top10(pond):
    # product_rank is this Pond's own table — SQL sees it directly.
    pond.write_table("top10", pond.con.sql("SELECT * FROM product_rank WHERE rank <= 10"))
```

The moving parts:

- **`@ripple`** registers a function as a [Ripple](../concepts/ripples.md); `@ripple(parents=[...])` orders it after other Ripples in the same Pond. Independent Ripples run in parallel.
- **`pond.read_table("sales.sale_line")`** reads a Source's published table (its exported Parquet snapshot). **`pond.read_table("product_rank")`** reads this Pond's own table, live.
- **`pond.con.sql(...)`** is a plain DuckDB connection — the full SQL surface is available, and Python variables holding relations (like `lines` above) can be referenced directly in queries.
- **`pond.write_table(name, relation)`** publishes a table atomically — a half-finished write is never visible, even to concurrent readers.

The complete handle API is in the [Python API reference](../reference/python-api.md).

## Inlets: ingesting external data

An Inlet's Ripples work the same way, minus Source reads — they fetch from the outside world (an API, a warehouse, files) and `write_table` the result. The demo `transactions` Pond is a worked example (it appends a synthetic batch each run, building on its own previous output via `pond.read_table("transaction")`). For sources that update on a known rhythm, pair the Inlet with a [Window](windows.md) so downstream Ponds only re-run when fresh data can actually exist.

## Test locally

The Pond reads `sales.sale_line`, which only exists on the Catchment — so define a Puddle for it in `src/puddles.py` that pulls a sample down:

```python
from duckstring import puddle


@puddle("sales.sale_line")
def sale_line(p):
    p.write_table(p.catchment().get())
```

Then run the Pond against it, entirely locally:

```bash
duckstring pond hydrate
duckstring pond run
duckstring puddle show top_sellers.top10
```

Your transform runs against real upstream data before it's ever deployed. Synthetic and file-based Puddles, single-Ripple runs, and incremental testing are covered in [Local Testing](local-testing.md).

## Deploy and run

```bash
duckstring pond deploy
duckstring trigger pulse top_sellers
```

The Pulse runs the whole lineage — `transactions`, `products`, `sales`, then `top_sellers` — and the live status view follows it through. From here:

```bash
duckstring query top_sellers top10
```

See [Deploying](deploying.md) for versioned upgrades, and [Triggers](triggers.md) for keeping the Pond continuously supplied.
