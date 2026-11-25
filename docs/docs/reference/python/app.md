---
title: App Handle
description: The read-only handle an App uses to read its Sources, and its Records and Media.
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

[`reports`](pond.md#reports) returns every report its Sources hold, since an App has no Run window.

`read_delta`, `trickle`, `write_table`, `append_table`, `merge_table`, `apply_zset`, `write_object`, `skip` and `report` raise `TypeError`. An App can't publish data, and has no Pond Run window to read changes over.

## `freshness`

```python
pond.freshness() -> dict
```

Returns the App's freshness and each Source's, as the Catchment last reported them, for showing how current the data is, and when each Source's data last changed:

```python
{"freshness": datetime(2026, 11, 9, 20, 51, tzinfo=timezone.utc),
 "sources": {"reports": datetime(...)},
 "changed": {"reports": datetime(...)}}
```

A Source's freshness moves on every Run, including Runs that found nothing to change. Its `changed` time moves only when its data did, so an App that caches what it builds from a Source can rebuild when `changed` moves.

The values are timezone-aware UTC. Under `pond run` it returns `{"freshness": None, "sources": {}, "changed": {}}`.

## `hold`

```python
with pond.hold():
    ...
```

Keeps the versions each Source has published now readable while the block runs. A read inside a request needs no hold: the Catchment keeps what a request may read until its response has been sent. Use it for reads outside a request, such as in a background thread or a cache filled at startup. Under `pond run` it does nothing.

## Records

An App with `[app] records = true` has its own tables in PostgreSQL. See [Records and Media](../../guides/records_and_media.md).

### `records_url`

```python
app.records_url() -> str
```

Returns the App line's Records as a standard PostgreSQL connection URL, signing in as the line's own role. The role's search path puts its own schema, `{name}_v{major}`, first, so unqualified table names are its own. Every App line's tables can be read as `{name}_v{major}.{table}`, and only its own can be written. Use it with any driver, such as psycopg, SQLAlchemy (as `postgresql+psycopg://`) or asyncpg. The same URL is in `DUCKSTRING_RECORDS_URL`.

**Raises** `RuntimeError` when the App doesn't declare `records = true`.

### `records`

```python
app.records(**kwargs) -> psycopg.Connection
```

Returns a psycopg connection to the App line's Records. Keyword arguments go to `psycopg.connect`. Needs psycopg in the App's environment (`uv add 'psycopg[binary]'`).

### `records_pool`

```python
app.records_pool(**kwargs) -> psycopg_pool.ConnectionPool
```

Returns a psycopg connection pool to the App line's Records, holding at most the instance's `[app] connections` (which defaults to `concurrency`, 4). The line's role may hold `instances` × `connections` connections in all (see [Connections](../../guides/scaling_apps.md#connections)). Keyword arguments go to `ConnectionPool`, such as `open=True`. Needs `psycopg_pool` in the App's environment (`uv add 'psycopg[binary]' psycopg_pool`).

## Media

An App with `[app] media = true` has its own folder of files. See [Records and Media](../../guides/records_and_media.md#keeping-files-in-media).

### `media`

```python
app.media() -> fsspec.AbstractFileSystem
```

Returns an [fsspec](https://filesystem-spec.readthedocs.io) filesystem rooted at the App line's Media, the same wherever the Catchment keeps it. Paths are relative, such as `fs.open("uploads/a.png", "wb")` or `fs.ls("uploads")`, and folders are made as needed. Needs fsspec in the App's environment, and the object store's own package (`s3fs`, `gcsfs` or `adlfs`) when Media is in one.

### `media_path`

```python
app.media_path() -> pathlib.Path
```

Returns the App line's Media as a local folder, for libraries that want a path.

**Raises** `RuntimeError` when the Catchment keeps Media in an object store; use `media()` there.

### `media_url`

```python
app.media_url() -> str
```

Returns where the App line's Media is: a local folder, or an object-store URI. The same value is in `DUCKSTRING_MEDIA_URL`.

**Raises** `RuntimeError` when the App doesn't declare `media = true`.

## `report_load`

```python
app.report_load(load) -> None
```

Tells the Catchment how busy this instance is, from `0` (idle) to `1` (full), for what no outside measure sees, such as a lock or a queue of the App's own. Above `[app.scale] load` (0.7) for `after`, the App gets another instance (see [Scaling Apps](../../guides/scaling_apps.md#reporting-load)). A report counts for 30 seconds. A response header `X-Duckstring-Load: 0.9` does the same. Outside a Catchment it does nothing.
