---
title: Ingesting Data
description: Bring data into Duckstring with Inlets, and skip runs when nothing new has arrived.
---

# Ingesting Data

Data enters a pipeline through Inlets: Ponds with no Sources, whose Ripples read from systems outside Duckstring. Here is the demo `products` Inlet reduced to its essentials, loading a catalog export from object storage:

```toml
[pond]
name = "products"
version = "1.0.0"
type = "inlet"
```

```python
from duckstring import ripple


@ripple
def ingest(pond):
    pond.write_table("product", pond.con.sql("""
        SELECT id, name, category, unit_price
        FROM read_csv('s3://acme-exports/catalog/products.csv')
    """))
```

Deploy it like any other Pond, and the Ponds that list `products` in `[sources]` read `products.product`.

This guide covers reading from common systems, choosing how to store what you read, pulling only new data, and skipping runs when nothing has changed. It assumes you've read [Writing Ripples](writing_ripples.md).

## Inlets

An Inlet is the only kind of Pond that can tell whether the outside world has changed. Every other Pond decides whether to run by comparing its Sources' freshness with its own, but an Inlet has no Sources to compare, so it runs whenever demand reaches it. That makes it the right place to check cheaply for new data, as covered [below](#skipping-when-nothing-changed).

An Inlet's freshness is the time the run was requested, available to its Ripples as `pond.f`. With a [window](#when-it-runs), it's the end of the window instead.

## Reading from outside

DuckDB reads most sources directly, so an Inlet is often a single query.

Files in object storage or over HTTP are read with `read_parquet`, `read_csv` and `read_json`, which accept `s3://`, `gs://` and `https://` paths and globs. DuckDB's `httpfs` extension handles the transfer, and loads itself on first use. For S3, give it credentials with a secret, here taken from the standard AWS credential chain:

```python
@ripple
def ingest(pond):
    pond.con.execute("CREATE SECRET IF NOT EXISTS acme (TYPE s3, PROVIDER credential_chain)")
    pond.write_table("product", pond.con.sql(
        "SELECT * FROM read_parquet('s3://acme-exports/catalog/*.parquet')"
    ))
```

Databases are read with DuckDB's `postgres` and `mysql` extensions. Attach the database read-only and query its tables:

```python
import os


@ripple
def ingest(pond):
    pond.con.execute(f"""
        ATTACH IF NOT EXISTS '{os.environ["SHOP_DATABASE_URL"]}' AS shop (TYPE postgres, READ_ONLY)
    """)
    pond.write_table("customer", pond.con.sql("SELECT id, name, region FROM shop.public.customer"))
```

An attached database is shared by all of the Pond's Ripples, so attach it under one name and use `IF NOT EXISTS`. Only tables in the Pond's own `main` schema are published, never the attached ones.

For an HTTP API, fetch in Python and turn the result into a relation. Duckstring installs PyArrow, so a list of records converts directly:

```python
import json
import urllib.request

import pyarrow as pa


@ripple
def ingest(pond):
    with urllib.request.urlopen("https://api.example.com/v1/rates?base=EUR") as resp:
        rates = json.load(resp)["rates"]
    records = [{"currency": code, "rate": rate} for code, rate in rates.items()]
    pond.write_table("exchange_rate", pond.con.from_arrow(pa.Table.from_pylist(records)))
```

Pass the relation to `write_table` as shown. Don't refer to a Python variable by name in SQL (`FROM records`): DuckDB finds such variables by scanning the call stack, which is unreliable when Ripples run in parallel.

Ripple code reads credentials from its environment. A Duck running on the Catchment's machine inherits the Catchment's environment variables, so set them where the Catchment starts. A Duck in the cloud doesn't inherit them, and gets AWS access from its task or instance role instead; see [Cloud Compute on AWS](cloud_compute_on_aws.md).

## Choosing a table type

How you write what you read decides what downstream Ponds can do with it.

| The source provides | Write it with | Downstream sees |
|---|---|---|
| A full snapshot, such as a catalog export or a reference list | `write_table` | The whole table, each time it's republished |
| New records only, such as orders, events or log lines | `append_table` | Just the records added since it last read |
| The current state of records that change in place, such as customers or prices | `merge_table` | Just the records inserted, updated or deleted |

Append and merge tables are [Trickles](../concepts/trickles.md): they keep a record of their changes, so downstream Ponds can process only what changed. [Append and Merge Tables](append_and_merge.md) covers both. A merge works out the changes itself by comparing the state you pass with the previous one, which suits a snapshot of a table whose rows change: the demo `catalog` Inlet merges a full product list on every run, and downstream sees only the products whose price moved.

## Incremental pulls

When a source can be asked for only the records after some point, an Inlet can read its own previous output to find that point. The demo `orders` Inlet works this way, continuing from the highest `order_id` it has. For a source with a timestamp:

```python
@ripple
def ingest(pond):
    since = None
    if pond.con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'event'").fetchone()[0]:
        since = pond.con.execute(
            "SELECT max(created_at) FROM event WHERE _duckstring_f < ?", [pond.f]
        ).fetchone()[0]
    new = pond.con.sql("SELECT * FROM read_parquet('s3://acme-events/*.parquet')")
    if since is not None:
        new = new.filter(f"created_at > '{since}'")
    pond.append_table("event", new)
```

The high-water mark only counts rows from earlier runs. Every row `append_table` writes is stamped with the run's freshness in `_duckstring_f`, and a run that's retried, or resumed after a crash, keeps the same `pond.f`. A replay therefore computes the same mark, reads the same records, and `append_table` replaces the rows it wrote the first time instead of duplicating them.

## Skipping when nothing changed

An Inlet that finds nothing new can mark its run as producing no change with `pond.skip()`. The Ponds downstream then pass without running, so a pipeline whose sources haven't changed costs only its Inlet runs. The checks below go from cheapest to most thorough.

### Checking the source first

Many sources can say whether they've changed without sending their data: an HTTP `ETag` or `Last-Modified` header, a file listing with modification times, or `max(updated_at)` on a source table. Record what you saw in a table of your own, and skip when it matches:

```python
import urllib.request

URL = "https://exports.acme.example/catalog/products.csv"


@ripple
def ingest(pond):
    with urllib.request.urlopen(urllib.request.Request(URL, method="HEAD")) as resp:
        etag = resp.headers["ETag"]
    pond.con.execute("CREATE TABLE IF NOT EXISTS _duckstring_seen (url VARCHAR, etag VARCHAR)")
    seen = pond.con.execute("SELECT etag FROM _duckstring_seen WHERE url = ?", [URL]).fetchone()
    if seen and seen[0] == etag:
        pond.skip()
        return
    pond.write_table("product", pond.con.sql(f"SELECT * FROM read_csv('{URL}')"))
    pond.con.execute("DELETE FROM _duckstring_seen WHERE url = ?", [URL])
    pond.con.execute("INSERT INTO _duckstring_seen VALUES (?, ?)", [URL, etag])
```

Tables whose names start with `_duckstring_` stay in the Pond's database and are never published.

### Comparing what you loaded

When the source can't say whether it changed, load it and compare with what was published:

```python
@ripple
def ingest(pond):
    pond.write_table("product", pond.con.sql(
        "SELECT * FROM read_csv('s3://acme-exports/catalog/products.csv')"
    ), if_changed=True)
```

With `if_changed=True`, `write_table` fingerprints the new table and compares it with the published one. When they match, nothing is written, the published table keeps the freshness of the run that last changed it, and downstream Ponds read it as unchanged. The fingerprint costs one scan of the new data. That's usually worth it when the table often comes back the same, because downstream Ponds can then skip work that reads it, and a [Trickle builder](joins_with_the_builder.md) that joins it stays incremental instead of recomputing in full.

Merge and append tables need none of this. A merge records only what changed, and `merge_table` and `append_table` return whether they changed anything.

To skip the whole run when nothing changed, ask in a Ripple that runs after the ones that write:

```python
@ripple(parents=[load_rates, load_products])
def done(pond):
    if not pond.changed():
        pond.skip()
```

`pond.changed()` reports whether this run changed any of the Pond's tables or Objects. A table written with `if_changed=True` whose content was the same doesn't count, nor does a merge or append that changed nothing.

The fingerprint is a hash, so two different tables could in principle match. To rule that out for a table, check it with `pond.detect_change("product", rel, exact=True)` before writing, which compares the rows themselves. See [Change detection](../reference/python/pond.md#change-detection) for the details, including the cases where a check always reports a change.

## Files and models as Objects

An Inlet that downloads a file that isn't a table, such as a model or a shapefile, can publish it as an [Object](writing_ripples.md#outputs-that-arent-tables). With `if_changed=True`, it's published only when its content differs from the published one:

```python
import tempfile
import urllib.request
from pathlib import Path


@ripple
def fetch_model(pond):
    path = Path(tempfile.mkdtemp()) / "demand_model.onnx"
    urllib.request.urlretrieve("https://models.acme.example/demand/latest.onnx", path)
    pond.write_object("demand_model", path, if_changed=True)
```

Objects are compared by a SHA-256 of their content, so there's no fingerprint to configure.

## When it runs

An Inlet runs when demand from downstream reaches it. Put [triggers](scheduling.md) on the Outlets that consume the data, and the Inlets run as often as those consumers need. The [Playground](https://playground.duckstring.com) shows this on the demo pipeline in the browser.

A source that only updates at known times, such as an export that lands nightly, should have a window on its Inlet, so it runs at most once per update:

```bash
duckstring trigger window products add --name nightly --every 1d --start 02:00 --duration 1h
```

See [Batch sources](scheduling.md#batch-sources).

## Testing an Inlet locally

An Inlet has no Sources, so it needs no Puddles: `duckstring pond run` reads from the real source. Point it at a test location through an environment variable if the real one is large or shouldn't be read from your machine.

In a local run, `pond.skip()` does nothing, and a full `pond run` starts from an empty output, so `if_changed=True` always writes. `pond.changed()` on the Pond's own tables works as it does when deployed, so a terminal Ripple's decision can be checked by printing it. See [Testing with Puddles](testing_with_puddles.md).
