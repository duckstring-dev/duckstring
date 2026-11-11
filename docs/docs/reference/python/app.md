---
title: App Handle
description: The read-only handle an App uses to read its Sources.
---

# App Handle

An [App](../pond_toml.md#app) reads the tables and Objects its Sources publish through a read-only handle:

```python
from duckstring import app

pond = app.pond()
rel = pond.read_table("reports.monthly_summary")
```

`app.pond()` returns the same handle every time it's called in a process, so call it once at module level. It works in a process the Catchment started, and under [`duckstring pond run`](../cli/pond.md), where it reads the App's Puddles. Anywhere else it raises `RuntimeError`.

The handle is a [Pond Handle](pond.md) with the differences below. See [Building Apps](../../guides/building_apps.md) for how an App uses it.

## Attributes

| Attribute | Type | Description |
|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | A cursor on the handle's in-memory DuckDB. Each thread gets its own cursor, so a web server's request threads don't share one. Its `memory_limit` is the App's `[pond] memory`, or 256 MB without one. |
| `name` | `str` | The App's name. |
| `version` | `str` | The deployed version. |

## Reading

`read_table`, `count_table`, `read_object` and `object_path` work as on the [Pond Handle](pond.md#reading-tables), with these differences:

- Every reference names a Source, as in `"reports.monthly_summary"`. A bare name raises `ValueError`, since an App has no tables of its own.
- A read returns the newest data the Source has published at the time of the call. Two reads in one request can see different versions if the Source publishes between them.
- `read_table` registers the Source table as a view on the calling thread's cursor. Run SQL that uses the view's name on `pond.con` in the same thread.

`read_delta`, `trickle`, `write_table`, `append_table`, `merge_table`, `apply_zset`, `write_object` and `skip` raise `TypeError`. An App can't publish data, and has no Pond Run window to read changes over.

## `freshness`

```python
pond.freshness() -> dict
```

Returns the App's freshness and each Source's, as the Catchment last reported them, for showing how current the data is:

```python
{"freshness": datetime(2026, 11, 9, 20, 51, tzinfo=timezone.utc), "sources": {"reports": datetime(...)}}
```

The values are timezone-aware UTC. Under `pond run` it returns `{"freshness": None, "sources": {}}`.

## `hold`

```python
with pond.hold():
    ...
```

Keeps the versions each Source has published now readable while the block runs. A read inside a request needs no hold: the Catchment keeps what a request may read until its response has been sent. Use it for reads outside a request, such as in a background thread or a cache filled at startup. Under `pond run` it does nothing.
