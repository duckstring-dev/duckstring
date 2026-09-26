---
title: Triggers
description: Tap, Wave, Pulse, Tide — the demand signals.
---

# Triggers

Deployed Ponds don't run until something demands they run. Triggers are those demand signals — two flavours ([pull and push](../concepts/freshness.md)), each available as a one-shot or a standing arrangement:

| | Once | Standing |
|---|---|---|
| **Push** | **Pulse** | **Tide** |
| **Pull** | **Tap** | **Wave** |

Triggers are usually sent to [Outlets](../concepts/ponds.md) — demand naturally enters where data is consumed — but any deployed Pond can be targeted. All four commands take `-c` to pick the Catchment and `-m`/`-v` to target a specific major or version.

Each trigger command opens the live status view focused on the target's lineage. One-shots (Pulse, Tap) close it when the pipeline settles; standing triggers (Wave, Tide) keep it open until `Ctrl+C` — closing the view never cancels the trigger. `--silent` skips the view entirely.

## Pulse — push once

```bash
duckstring trigger pulse reports
```

A Pulse pushes `reports` to *now*: every Pond in its lineage runs once, in dependency order, and the cascade lands back at `reports` with fully fresh data. This is the "just run it" button — the right tool for ad-hoc refreshes and irregular workloads.

## Tide — standing push

```bash
duckstring trigger tide reports 4h
```

A Tide keeps a Pond's staleness under a **bound** — here, `reports` is never more than 4 hours old. The bound takes compound durations (`30s`, `90m`, `1d`, `1h30m`).

A Tide is not a schedule. "Run every 4 hours" and "never be more than 4 hours stale" coincide when everything is healthy, but diverge usefully when it isn't: the staleness bound expresses the actual requirement, and the runtime arranges the work to honour it.

## Tap — pull once

```bash
duckstring trigger tap reports
```

A Tap asks for one resupply: `reports` runs as soon as its Sources hold fresher data, and pull tokens propagate upstream to make that happen. The distinctive part is the re-arming — as each Pond starts, it pulls on its own Sources, so the whole lineage begins preparing the *next* generation while this one is still flowing down. A second Tap shortly after the first is typically supplied almost instantly.

That makes Tap a natural fit for consumption-driven refresh: have the application emit a Tap whenever it reads the Outlet, and the pipeline keeps pace with actual usage — idle when nobody's looking.

## Wave — standing pull

```bash
duckstring trigger wave reports
```

A Wave is a Tap that renews itself every time the Pond starts, so the pipeline free-runs. Its cadence is *emergent*: every Pond settles into the rhythm of the slowest Ripple upstream (the demo pipeline waves at ~3 s, the duration of `sales.join_lines`), with multiple runs in flight at once and no Pond ever outrunning its consumer.

Use a Wave whenever data should simply be as fresh as possible. To make a Wave run *on a clock* rather than flat-out, don't reach for a schedule — put a [Window](windows.md) on the Inlets. A Wave downstream of a daily Window runs the pipeline exactly once a day, at the moment fresh source data actually exists.

## Removing a standing trigger

```bash
duckstring trigger remove reports
```

Drops the Pond's standing Wave or Tide. It's graceful: demand already in the graph drains naturally, then everything goes idle. (A Pond holds at most one standing trigger — setting a new one replaces the old.) To stop a Pond more abruptly, see [Control](control.md) — `sleep` clears demand immediately, `kill` terminates execution.

## Choosing

- **Run it now, once** → **Pulse**.
- **Keep it no staler than X** → **Tide** with bound X.
- **Refresh when consumed** → **Tap**, sent on consumption.
- **As fresh as possible, always** → **Wave**.
- **As fresh as the source actually updates** → **Wave** + a [Window](windows.md) on the Inlet.

As a rule of thumb from the [demand model](../concepts/freshness.md): push suits irregular, occasional runs; pull suits continuous operation, where its bottleneck-throttling does the capacity planning for you.
