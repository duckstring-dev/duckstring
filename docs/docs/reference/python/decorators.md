---
title: Decorators
description: "@ripple and @puddle."
---

# Decorators

Both decorators register the function they wrap when the module is imported. Duckstring imports a Pond's entrypoints (by default `src/pond.py` and `src/puddles.py`, configurable in [`pond.toml`](../pond_toml.md)) to discover them.

```python
from duckstring import ripple, puddle
```

## `@ripple`

```python
ripple(func=None, *, parents=None, name=None, always_run=False, memory=None)
```

Registers a function as a [Ripple](../../concepts/ripples.md). Can be used bare (`@ripple`) or with arguments (`@ripple(parents=[...])`).

The decorated function takes one argument, the [Pond handle](pond.md), and its return value is ignored. Its outputs are whatever it writes through the handle.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `parents` | list of functions or names | `[]` | Ripples in the same Pond that must finish before this one starts, given as function references or as names. A name can also refer to a [SQL Ripple](../../guides/sql_ripples.md) declared in `pond.toml`. Dependencies on other Ponds are declared in `pond.toml`, never here. |
| `name` | `str` | the function name | The Ripple's name, used in run history, the UI and `duckstring pond run --ripple`. |
| `always_run` | `bool` | `False` | Run even when no Source has changed since the last Pond Run. Without it, such a run is skipped. Set this on a Ripple with side effects that must happen every run. If any Ripple in a Pond sets it, the whole Pond always runs. |
| `memory` | size or `int` | `pond.toml`'s `[pond] memory`, else none | The memory the Ripple holds outside DuckDB, such as a pandas DataFrame or a model, as a size (`"6GB"`, `"512MB"`, `"2GiB"`) or bytes. The Catchment reserves it on the machine while the Pond runs, and leaves it out of DuckDB's share. Work done in DuckDB needs none: DuckDB keeps to its share and spills to disk. |

Ripples with no parents in common run in parallel. A Ripple runs at most once at a time, but Ripples from consecutive Pond Runs can overlap.

A parent that isn't a Ripple in the Pond, a duplicate name, or parents that form a cycle raise `duckstring.core.RippleDeclarationError` (a subclass of `ValueError`) when the Pond is deployed or run locally.

```python
from duckstring import ripple

@ripple
def daily_sales(pond):
    ...

@ripple
def price_tiers(pond):
    ...

@ripple(parents=[daily_sales, price_tiers])
def join_lines(pond):
    ...
```

A Ripple with `always_run=True` can still skip its data work when nothing upstream changed:

```python
@ripple(always_run=True)
def notify(pond):
    send_heartbeat()                 # happens every run
    if not pond.sources_changed():
        pond.skip()
        return
    ...                              # only when a Source changed
```

To act on one Source table only, ask about it with [`changed`](pond.md#changed):

```python
@ripple(always_run=True)
def notify(pond):
    send_heartbeat()
    if not pond.changed("sales.sale_line"):
        pond.skip()
        return
    ...                              # only when sale_line changed
```

## `@puddle`

```python
puddle(target, *, set=None)
```

Registers a function that builds a [Puddle](../../concepts/ponds.md#puddles): a local stand-in for Source data, used by `duckstring pond hydrate` and `duckstring pond run`.

| Parameter | Type | Description |
|---|---|---|
| `target` | `str` | Either `"source.table"`, for one table of a Source, or `"source"`, for a whole Source whose tables the function names itself. |
| `set` | `str` or tuple of `str` | The Puddle set the definition belongs to, or several. Without it, the definition is in the default set. |

The decorated function takes one argument, the [Puddle handle](puddle.md). It can write its data through the handle, or return it: a returned relation is written as the target table, and a returned path is copied in with `write_path`. A Puddle may also target the Pond's own name, to provide its previous output for testing incremental runs.

```python
from duckstring import puddle

@puddle("transactions.transaction")
def transactions(p):
    return p.read_source().filter("store_id = 7")

@puddle("products")
def products(p):
    p.write_table("product", p.catchment().get("product"))
```

A definition that reads its Source with [`p.read_source`](puddle.md#read_source) can be read live and replayed through the Source's history (`pond hydrate --live`, `--as-at`), so it must give the same rows for the same data: a filter, a join or a sample by key hash, not a random sample.

A Source with no Puddle definition is skipped with a warning when hydrating, unless `--live`, `--as-at` or `--from-catchment` reads it from the Catchment.

### Puddle sets

Sets keep several versions of a Pond's Puddles side by side, such as a small sample and a full week. `pond hydrate --set NAME`, or a Source's entry in [`puddles.toml`](../cli/pond.md#puddlestoml), picks the set. For each target, the set's definition is used where it has one and the default set's otherwise. A whole-Source definition in a set replaces all of that Source's default ones. `default` names the unlabelled definitions, so it can't be used as a label, and two definitions for one target in one set are an error.

```python
@puddle("transactions.transaction", set=("recent", "full"))
def transactions(p):
    ...
```

## `@puddle.step`

```python
puddle.step(target, *, set=None)
```

Registers a function that writes the next window of a Puddle, for `duckstring pond run --incremental`. A step runs when the run's target lies beyond the history a Source's Puddle holds, so a test can go on past the end of real data, or run on generated data alone. Steps apply to copied Puddles, not live ones.

| Parameter | Type | Description |
|---|---|---|
| `target` | `str` | As for `@puddle`. |
| `set` | `str` or tuple of `str` | As for `@puddle`. |

A step writes through the same handle as a definition, usually with `append_table` or `merge_table`, and sees `p.previous_f`, `p.f` and `p.step`. Seed generated data with `p.step`, so a rerun from the baseline writes the same rows. A step that writes nothing still counts: the Source ran and didn't change. For a test on generated data alone, give every Source a step.

```python
@puddle.step("orders.order_line")
def more_orders(p):
    p.append_table(p.con.sql(f"SELECT {p.step} * 100 + range AS order_id FROM range(5)"), pk="order_id")
```
