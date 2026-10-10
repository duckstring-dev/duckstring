# Duckstring

**Get your ducks in a row.**

Duckstring is an open source data engineering platform built on DuckDB. Data into the terabytes fits comfortably on a single machine, which is where DuckDB is at its best, and Duckstring provides what a pipeline needs around it: packaging and versioning, orchestration, incremental processing, a catalog and delivery. It runs the same on a laptop, on a single server, or with work spread across cloud machines.

[Documentation](https://docs.duckstring.com) · [Quickstart](https://docs.duckstring.com/quickstart) · [Playground](https://playground.duckstring.com)

## Versioned transformations

Each set of transformations is a *Pond*: a Python project whose `pond.toml` names it, gives it a semantic version and lists the Ponds it reads from.

```toml
[pond]
name = "sales"
version = "1.2.0"

[sources]
transactions = "1.0.0"
products = "1.1.0"
```

The pipeline follows from those declarations, so it never has to be drawn by hand. A breaking change ships as a new major version, which runs alongside the old one while downstream Ponds move across at their own pace.

## Pull orchestration

Schedules are set on the Ponds whose output is actually used, at the end of the pipeline. Duckstring works back from there, running each upstream Pond only as often as something downstream needs it, so a path nobody consumes never runs. A daily job is one command:

```bash
duckstring trigger tide reports 1d
```

The [Playground](https://playground.duckstring.com) runs the demo pipeline in your browser, with a short guided tour of how it's scheduled.

## Incremental processing

A *Trickle* stores a table's changes as a Z-set: rows weighted +1 when added and -1 when removed. Joins and aggregations written with the Trickle builder recompute only the keys that changed since the last run, and handle updates and deletes correctly across any shape of join:

```python
from duckstring import ripple


@ripple
def priced_line(pond):
    (
        pond.trickle("orders.order_line")
        .join(pond.trickle("catalog.product"), on="product_id")
        .select(
            "s0.order_id, s0.product_id, s0.quantity, s1.unit_price, "
            "CAST(round(s0.quantity * s1.unit_price, 2) AS DECIMAL(14,2)) AS revenue"
        )
        .merge("priced_line", pk="order_id")
    )
```

## Code and data together

The runtime, called the *Catchment*, is also the catalog. Each major version of a Pond is a schema holding its tables, with lineage, run history and the version's schema contract recorded alongside. Query it from the CLI, from Python, or from any Postgres client:

```sql
SELECT * FROM reports_v1.monthly_summary;
```

## Python and the command line

Ripples, the steps inside a Pond, are Python functions with a DuckDB connection, so SQL, DuckDB's relation API, pandas and Arrow all work. Everything a Catchment does can be driven from the `duckstring` CLI or its HTTP API, and a web UI shows pipelines as they run.

## Quickstart

Duckstring needs Python 3.10 or newer.

```bash
pip install duckstring
duckstring catchment init --name dev
```

This starts a local Catchment, with its web UI at http://127.0.0.1:7474. Leave it running, and in a second terminal:

```bash
mkdir demo && cd demo
duckstring pond demo                       # transactions, products → sales → reports
duckstring pond deploy --all --yes
duckstring trigger pulse reports           # run the pipeline once, from the end
duckstring query reports monthly_summary
```

The [Quickstart](https://docs.duckstring.com/quickstart) goes through the same steps with explanations.

## Also included

- [Local testing](https://docs.duckstring.com/guides/testing_with_puddles) against small samples of each Source, called Puddles
- [dbt projects](https://docs.duckstring.com/guides/dbt_projects) deployed as Ponds, one step per model
- [Delivery](https://docs.duckstring.com/guides/delivering_with_spouts) to Postgres (incremental and exactly once), S3, Google Cloud Storage and local files
- [Retries, alerts and Prometheus metrics](https://docs.duckstring.com/guides/monitoring_and_failures)
- [Per-Pond Python environments](https://docs.duckstring.com/guides/writing_ripples#python-dependencies) from `pyproject.toml` and `uv.lock`
- [Cloud compute](https://docs.duckstring.com/guides/cloud_compute_on_aws) on AWS Fargate or EC2, chosen per Pond, with data on S3
- [Connected Catchments](https://docs.duckstring.com/guides/connecting_catchments), where a Pond reads from a Pond on another Catchment

## Documentation

[docs.duckstring.com](https://docs.duckstring.com) has the concepts, task guides, and a reference for the CLI, Python API, `pond.toml` and HTTP API. To try pull orchestration without installing anything, the [Playground](https://playground.duckstring.com) simulates it in the browser, with a guided tour.

## Status

Duckstring is pre-1.0, so interfaces may still change between minor versions. Changes are listed in the [changelog](CHANGELOG.md).

## Hosted Catchments

A hosted Catchment service is planned at [duckstring.com](https://duckstring.com). If you're interested, get in touch at [dev@duckstring.com](mailto:dev@duckstring.com).

## License

[Apache 2.0](LICENSE)
