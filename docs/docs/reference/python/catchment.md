---
title: Catchment Client
description: Reading published data from Python.
---

# Catchment Client

```python
from duckstring import Catchment
```

A small client for reading a Pond's published tables from a running Catchment. Results come back as DuckDB relations, so they can be queried further, converted to a DataFrame, or written out.

```python
c = Catchment("http://127.0.0.1:7474")
summary = c.get("monthly_summary", pond="reports")
print(summary.df())
```

## `Catchment`

```python
Catchment(url, con=None, default_pond=None, default_table=None, api_key=None, headers=None)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | | The Catchment's address. |
| `con` | `duckdb.DuckDBPyConnection` | a new in-memory connection | Where results are loaded. |
| `default_pond` | `str` | `None` | Pond used when a method is called without `pond`. |
| `default_table` | `str` | `None` | Table used when `get` is called without `table`. |
| `api_key` | `str` | `None` | Sent as `Authorization: Bearer <key>`, unless `headers` already sets `Authorization`. |
| `headers` | `dict[str, str]` | `None` | Extra headers sent with every request, for a hosting platform that authenticates requests itself. |

Queries always target the Pond's highest deployed major version.

Inside a `@puddle` function, [`p.catchment()`](puddle.md#catchment) returns one of these already configured for the Puddle's Source, using the Catchment registered with the CLI.

## Methods

### `query`

```python
c.query(sql, pond=None) -> duckdb.DuckDBPyRelation
```

Runs read-only SQL against one Pond's published tables on the Catchment and returns the result. Tables are referred to by bare name (`FROM monthly_summary`).

| Parameter | Type | Description |
|---|---|---|
| `sql` | `str` | The query. |
| `pond` | `str` | The Pond. Defaults to `default_pond`. |

**Raises** `RuntimeError` when the Catchment returns an error, and `ValueError` with no Pond given.

### `get`

```python
c.get(table=None, pond=None) -> duckdb.DuckDBPyRelation
```

Fetches a whole table. Equivalent to `query('SELECT * FROM "table"')`.

**Raises** `ValueError` with no table given.

### `tables`

```python
c.tables(pond=None) -> list[str]
```

Returns the names of a Pond's published tables.
