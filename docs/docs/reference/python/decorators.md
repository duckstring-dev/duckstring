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
ripple(func=None, *, parents=None, name=None, always_run=False)
```

Registers a function as a [Ripple](../../concepts/ripples.md). Can be used bare (`@ripple`) or with arguments (`@ripple(parents=[...])`).

The decorated function takes one argument, the [Pond handle](pond.md), and its return value is ignored. Its outputs are whatever it writes through the handle.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `parents` | list of functions | `[]` | Ripples in the same Pond that must finish before this one starts, given as function references. Dependencies on other Ponds are declared in `pond.toml`, never here. |
| `name` | `str` | the function name | The Ripple's name, used in run history, the UI and `duckstring pond run --ripple`. |
| `always_run` | `bool` | `False` | Run even when no Source has changed since the last Pond Run. Without it, such a run is skipped. Set this on a Ripple with side effects that must happen every run. If any Ripple in a Pond sets it, the whole Pond always runs. |

Ripples with no parents in common run in parallel. A Ripple runs at most once at a time, but Ripples from consecutive Pond Runs can overlap.

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

## `@puddle`

```python
puddle(target)
```

Registers a function that builds a [Puddle](../../concepts/ponds.md#puddles): a local snapshot of Source data used by `duckstring pond hydrate` and `duckstring pond run`.

| Parameter | Type | Description |
|---|---|---|
| `target` | `str` | Either `"source.table"`, for one table of a Source, or `"source"`, for a whole Source whose tables the function names itself. |

The decorated function takes one argument, the [Puddle handle](puddle.md).

```python
from duckstring import puddle

@puddle("transactions.transaction")
def transactions(p):
    p.write_table(p.con.sql("SELECT * FROM range(100) t(id)"))

@puddle("products")
def products(p):
    p.write_table("product", p.catchment().get("product"))
```

A Source with no Puddle definition is skipped with a warning when hydrating, unless `--from-catchment` is passed.
