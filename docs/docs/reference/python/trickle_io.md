---
title: Trickle I/O
description: Writing and reading incremental tables directly.
---

# Trickle I/O

These [Pond handle](pond.md) methods write and read [Trickles](../../concepts/trickles.md) directly. Most incremental Ripples are easier to write with the [Trickle Builder](trickle_builder.md), which calls these for you; use them directly for simple append or merge tables, or for hand-written incremental logic.

A Trickle stores two system columns alongside your own:

| Column | Description |
|---|---|
| `_duckstring_f` | The freshness of the run that wrote the row. Stamped on every history and change-log row. |
| `_duckstring_d` | The row's weight in a change: `+1` for added, `-1` for removed. Present on change-log rows and on the `.zset` of a [`Delta`](#delta). |

`read_table` strips both, so a consumer reading a Trickle in full sees an ordinary table.

## Writing

All three write methods stamp rows with `pond.f`, are safe to repeat at the same `f` (a retry or crash replay replaces that run's rows), and return whether anything changed, for use with [`pond.skip()`](pond.md#skip).

### `append_table`

```python
pond.append_table(name, relation, *, pk=None, fail_on_conflict=True, retain_t=None, retain_n=None) -> bool
```

Appends the rows of `relation` to the append Trickle `name`, creating it on first use. Rows are only ever added; nothing is compared with existing rows unless `pk` is set.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | | Table name. |
| `relation` | `duckdb.DuckDBPyRelation` | | The new rows. |
| `pk` | `str` or list of `str` | `None` | The table's primary key. Recorded for downstream consumers and the data viewer, and checked when `fail_on_conflict` is true. |
| `fail_on_conflict` | `bool` | `True` | With `pk` set, check that the key is unique within `relation` and not already in the table's history, and raise before writing if not. `False` skips the check. Has no effect when `pk` is unset. |
| `retain_t` | `timedelta` | `None` | Drop history rows stamped earlier than `f - retain_t`. |
| `retain_n` | `int` | `None` | Keep only the rows from the newest `retain_n` runs. |

**Returns** `True` if any rows were appended. An empty relation, or a replay of rows already written at this `f`, returns `False`.

**Raises** `DeltaError` on a primary key conflict, or if a `pk` column is missing from `relation`.

Retention bounds how far back a consumer can read changes. A consumer that falls behind the retained history reads the whole table instead, so retention never affects correctness.

```python
@ripple
def ingest(pond):
    batch = pond.con.sql("SELECT ... AS order_id, ... FROM ...")
    pond.append_table("order_line", batch, pk="order_id", fail_on_conflict=False)
```

### `merge_table`

```python
pond.merge_table(name, relation, *, pk, retain_t=None, retain_n=None, compact_threshold=None) -> bool
```

Merges the complete current state of a table into the merge Trickle `name`. Duckstring compares `relation` with the table's state before this run and records the difference (inserts, updates and deletes) in its change log. Pass the whole table every time; rows missing from `relation` are recorded as deleted.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | | Table name. |
| `relation` | `duckdb.DuckDBPyRelation` | | The table's complete current state. |
| `pk` | `str` or list of `str` | required | The primary key. Must be unique in `relation`. |
| `retain_t` | `timedelta` | `None` | Drop change-log rows stamped earlier than `f - retain_t`. The current state is never trimmed. |
| `retain_n` | `int` | `None` | Keep only the change-log rows from the newest `retain_n` runs. |
| `compact_threshold` | `int` (bytes) | `None` | Override the size the change log must reach before it is folded into the table's base. Defaults to `DUCKSTRING_COMPACT_THRESHOLD` (256 MiB). |

**Returns** `True` if the state changed.

**Raises** `DeltaError` if `pk` is empty or a `pk` column is missing from `relation`.

Within the Pond, `name` is a view over the table's current state, without system columns, so later Ripples can query it in SQL. The compacted base is stored as `{name}__base`. Don't write the same name with `write_table`.

```python
@ripple
def ingest(pond):
    state = pond.con.sql("SELECT product_id, name, category, unit_price FROM ...")
    pond.merge_table("product", state, pk="product_id")
```

### `apply_zset`

```python
pond.apply_zset(name, zset, *, pk, retain_t=None, retain_n=None, compact_threshold=None) -> bool
```

Appends an already-computed change to the merge Trickle `name`, without comparing it with the existing state. This is the low-level write the builder uses for incremental results. The change is consolidated first: weights of identical rows are summed and rows that sum to zero are dropped.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | | Table name. |
| `zset` | `duckdb.DuckDBPyRelation` | | The change: your columns plus `_duckstring_d`. An update is a `-1` row with the old values and a `+1` row with the new. |
| `pk` | `str` or list of `str` | required | The primary key. |
| `retain_t`, `retain_n`, `compact_threshold` | | | As for `merge_table`. |

**Returns** `True` if the consolidated change is non-empty.

**Raises** `DeltaError` if `pk` is empty.

Only use this when you have computed the change correctly yourself. A wrong weight corrupts the table's state for every consumer.

## Reading

### `read_delta`

```python
pond.read_delta(ref) -> Delta
```

Returns the changes to a Source table over this run's window, `(pond.previous_f, pond.f]`.

| Parameter | Type | Description |
|---|---|---|
| `ref` | `str` | `"source.table"`. |

What the delta contains depends on the Source table:

| Source table | Result |
|---|---|
| Append Trickle | The rows appended in the window, all weighted `+1`. |
| Merge Trickle | The change-log rows in the window, consolidated: weights of identical rows are summed and zero-weight rows dropped, so several changes to one row collapse to the net change. |
| Plain table (`write_table`) | If it was published after `previous_f`, the whole table weighted `+1` with `is_full=True`. Otherwise an empty delta. |
| Any table, on this Pond's first run or when `previous_f` is older than the Source's retained history | The whole table weighted `+1`, with `is_full=True`. |

**Raises** `ValueError` if `ref` has no Source prefix, and `MissingSourceAsset` if the table isn't published.

## `Delta`

```python
from duckstring.trickle.io import Delta
```

The result of `read_delta`.

| Member | Type | Description |
|---|---|---|
| `zset` | `duckdb.DuckDBPyRelation` | The changes: the Source's columns plus `_duckstring_d`. |
| `is_full` | `bool` | `True` when this is a full read rather than a window of changes. A consumer must then recompute its whole output from it instead of applying it as an increment. |
| `pk` | `tuple[str, ...]` | The Source table's primary key, if it declared one. |
| `upserts` | `duckdb.DuckDBPyRelation` | Rows present after the change (net weight above zero), without system columns. |
| `deletes` | `duckdb.DuckDBPyRelation` | Primary key values that were removed and not re-added. Empty when the Source has no primary key. |
| `is_empty()` | `bool` | Whether there are no changes. |
| `keys_count()` | `int` | The number of changed rows. |

```python
@ripple
def restock(pond):
    delta = pond.read_delta("orders.order_line")
    if delta.is_full:
        ...                             # rebuild everything from delta.zset
    elif not delta.is_empty():
        ...                             # apply delta.upserts and delta.deletes
```

## Errors

| Exception | Import from | Raised when |
|---|---|---|
| `DeltaError` | `duckstring.trickle.io` | A Trickle write is used incorrectly: a missing or conflicting primary key, or a write outside a run with no `pond.f`. A subclass of `ValueError`. |
| `MissingSourceAsset` | `duckstring.core` | A Source table isn't published. The Pond waits for the Source instead of failing. A subclass of `FileNotFoundError`. |
