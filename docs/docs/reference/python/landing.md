---
title: Change Files
description: Reading back the change files a Spout lands with --mode changes.
---

# Change Files

```python
from duckstring import landing
```

A [Spout](../../guides/delivering_with_spouts.md#change-files) with `--mode changes` lands each delivery as files: for every table that changed, a file of its changes or of the whole table, then a manifest naming those files. [Change files](../formats.md#change-files) describes the layout. These functions read a destination's deliveries back, from a local path, a `/Volumes/...` path or an object-store URI.

Each delivery has a **position**: its freshness as an ISO timestamp, with `#n` added for the n-th delivery again at a freshness already delivered (a resync). Positions order the deliveries. Keep the position of the last delivery you applied, and pass it back as `after`.

## `deliveries`

```python
landing.deliveries(path, *, after=None) -> list[dict]
```

The committed deliveries at `path`, oldest first, as their manifests.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | | The Spout's destination. |
| `after` | `str` or `datetime` | `None` | Only deliveries after this position, given as a position string or a freshness. `None` lists them all. |

A manifest that's still being written ends the list, so the list never skips a delivery.

Each manifest is a dict:

| Key | Description |
|---|---|
| `format` | The layout's version, `1`. |
| `pond`, `major` | The Pond line that published the data. |
| `delivery` | The delivery's position. |
| `f` | The delivery's freshness, as an ISO timestamp. |
| `previous_f` | The freshness of the delivery before it, or `None` for the first. |
| `tables` | One entry per table the delivery landed, below. |

| Table key | Description |
|---|---|
| `kind` | `"changes"` for the changes since the previous delivery, `"full"` for the whole table. |
| `pk` | The table's primary key, or `None`. |
| `column` | `"_duckstring_op"` or `"_duckstring_d"` for changes, `None` for a whole table. |
| `rows` | The number of rows landed. |
| `files` | The table's files, relative to `path`. |

## `changes`

```python
landing.changes(con, path, table, *, after=None) -> Changes | None
```

`table`'s changes in the deliveries after `after`, combined into one relation on `con`. Returns `None` when no delivery since touched the table.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | | The connection the relation is made on. |
| `path` | `str` | | The Spout's destination. |
| `table` | `str` | | The table's name. |
| `after` | `str` or `datetime` | `None` | Only deliveries after this position. |

Deliveries combine as follows:

- With a primary key, each changed key's row from the newest delivery that has it, with `_duckstring_op`: `1` with the key's row (insert or update), `-1` with its old row (delete).
- Without one, the weights of each row summed, with `_duckstring_d` holding the sum. Rows whose weights cancel out are left out.
- When a whole-table delivery is among them, from the newest such delivery on. `reset` is then true, and with a primary key only the `1` rows are returned.

Every row also carries `_duckstring_f`, the freshness of the delivery it came from (without a key, the newest of the deliveries it was combined from). On a Volume reached through the Files API, the files are fetched and loaded into a temporary table on `con`.

## `Changes`

| Attribute | Type | Description |
|---|---|---|
| `relation` | `duckdb.DuckDBPyRelation` | The combined changes, with the table's columns plus `_duckstring_f` and `_duckstring_op` or `_duckstring_d`. |
| `reset` | `bool` | A whole-table delivery is included, so `relation` holds the whole table: replace what you hold with it. |
| `pk` | list of `str` or `None` | The table's primary key. |
| `f` | `datetime` | The newest delivery's freshness. |
| `after` | `str` | The newest delivery's position. Pass it as `after` next time. |

This applies a merge table's changes to a copy in DuckDB:

```python
import duckdb

from duckstring import landing

con = duckdb.connect("replica.duckdb")
got = landing.changes(con, "/srv/landing/priced", "priced_line", after=last_applied)
if got is not None:
    rows = got.relation.sql_query()
    cols = "order_id, product_id, quantity, unit_price, revenue"
    if got.reset:
        con.execute("DELETE FROM priced_line")
    else:
        con.execute(f"DELETE FROM priced_line WHERE order_id IN (SELECT order_id FROM ({rows}))")
    con.execute(f"INSERT INTO priced_line SELECT {cols} FROM ({rows}) WHERE _duckstring_op = 1")
    last_applied = got.after
```
