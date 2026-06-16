---
title: Incremental Ripples
description: Process only new rows each run — the self-read pattern, why it's replay-safe, and how to full-refresh.
---

# Incremental Ripples

A Ripple recomputes its tables each run — but nothing says it must recompute them *from scratch*. A Pond's working database persists between runs, so a Ripple can read its own previous output, work out what's new, and append. This page is the supported pattern for that today; a first-class incremental construct (**Trickle**, which moves the watermark bookkeeping into the framework) is planned but not yet built.

## The self-read pattern

Three steps: cold-start if the table doesn't exist yet, compute the new slice past a watermark, append atomically.

```python
from duckstring import ripple


@ripple
def events(pond):
    pond.read_table("tracker.event")    # registers the Source table as the view `event`

    exists = pond.con.sql(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'events'"
    ).fetchall()
    if not exists:
        # Cold start: take everything.
        pond.write_table("events", pond.con.sql("SELECT * FROM event"))
        return

    new = pond.con.sql("""
        SELECT * FROM event
        WHERE ingested_at > (SELECT MAX(ingested_at) FROM events)
    """)
    pond.write_table("events", pond.read_table("events").union(new))
```

The watermark is yours: any monotonic column works (an ingestion timestamp, a sequence id). With a timestamp that can carry ties, prefer a strictly increasing key, or guard with an anti-join on the row's id instead of `>` alone.

The demo's `transactions` and `products` Inlets use this same shape to grow their tables run over run — an Inlet building on its own previous output is the pattern in its simplest form.

## Freshness as the watermark

When no data column fits, the framework provides one: **`pond.f`**, the run's [freshness](../concepts/freshness.md). It has a property wall-clock lacks: crash replay and immediate retries re-execute at the *same* F, so rows stamped with it are identical no matter how many attempts the run took — while an on-change retry is a genuinely new run at a new F, which is exactly the distinction a watermark wants.

Stamping with it gives a Source-to-Sink incremental protocol with no bespoke columns: the Source stamps what it publishes, the Sink takes everything fresher than what it has consumed.

```python
# In the Source — stamp each run's rows:
@ripple
def publish(pond):
    pond.write_table("event", pond.con.sql(
        f"SELECT *, TIMESTAMP '{pond.f.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS run_f FROM staged"
    ))

# In the Sink — consume only what's new:
@ripple
def consume(pond):
    pond.read_table("tracker.event")
    new = pond.con.sql("""
        SELECT * FROM event
        WHERE run_f > COALESCE((SELECT MAX(run_f) FROM events), TIMESTAMP '1970-01-01')
    """)
    pond.write_table("events", new if _cold(pond) else pond.read_table("events").union(new))
```

(`_cold` is the same `information_schema` existence check as above — the `COALESCE` already makes the filter cold-start-safe, so the branch only decides between create and append.)

## The `(previous_f, f]` bracket

The Sink above tracks "what it has consumed" itself. The framework hands you the same information directly: **`pond.previous_f`** is the freshness of the Sink's *own* previous successful run, so the rows a run should newly read from a Source are exactly the bracket **`(previous_f, f]`** — open at the bottom (already consumed), closed at the top.

```python
@ripple
def consume(pond):
    src = pond.read_table("tracker.event")  # registers the view `event`
    new = pond.con.sql(
        f"SELECT * FROM event "
        f"WHERE run_f >  TIMESTAMP '{pond.previous_f.strftime('%Y-%m-%d %H:%M:%S.%f')}' "
        f"  AND run_f <= TIMESTAMP '{pond.f.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
    )
    pond.write_table("events", new if _cold(pond) else pond.read_table("events").union(new))
```

On the first run `previous_f` is the far-past sentinel `NEVER`, so the lower bound lets everything through. Like `pond.f`, it is stable across retries and crash recovery.

Two things to keep in mind:

- **The upper bound `f` matters** — read *up to* your own freshness, not the Source's latest. A Source can independently run ahead of your coordination epoch; the closed top is the exactly-once ceiling that stops you over-reading rows from a future the run hasn't reached.
- Both bounds come from **your** freshness, not a per-edge watermark — which is why this composes without bookkeeping.

This is the protocol the planned **Trickle** construct will formalise (windowing the read automatically, and owning the stamp column under the reserved `_duckstring_*` namespace); using `pond.f` / `pond.previous_f` now means nothing to unlearn later.

## Why this is replay-safe

Two mechanics make the pattern exactly-once per run, even across crashes:

- **`write_table` is build-then-swap.** The new table materialises in full (reading the *old* `events` while doing so), then replaces it in one transaction. A run that dies mid-write leaves the previous state untouched.
- **Recovery re-runs only incomplete Ripples.** After a crash, the worker's ledger re-runs the Ripple from the start — and because the previous state survived, it recomputes the *same* append. There is no half-applied increment to double-apply.

`control force` composes sensibly too: a forced recompute re-reads the unchanged Source, finds nothing past the watermark, and appends nothing.

## Multiple Sources

With several inputs, keep one watermark per Source — usually just a `MAX(...)` per input column as above. If the bookkeeping grows beyond that, store it explicitly in a small state table the Ripple writes alongside its output (`pond.write_table("_watermarks", ...)`); it persists and replays by exactly the same rules.

## Full refresh

Sometimes you want to rebuild from nothing — after a logic fix that changes history, say.

- **Locally**, `duckstring pond run --fresh` ignores the self-puddle seed and starts cold.
- **On a deployed Pond** there is no built-in full-refresh verb yet. The operational route: make sure the Pond is idle (`duckstring control sleep`, and `kill` if a run is in flight), delete its working database — `ponds/{name}/m{major}/registry.duckdb` under the Catchment root — then `duckstring control force`. The next run finds no table and takes the cold-start branch. The published Parquet snapshot stays in place until that run completes, so Sinks keep reading consistent data throughout.

A `--full-refresh` control verb that does this safely in one step is on the roadmap alongside Trickle.
