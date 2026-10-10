---
title: Scheduling
description: Choose triggers and windows for common schedules.
---

# Scheduling

Duckstring schedules from the end of the pipeline: you put a trigger on the Pond whose data someone uses, and everything upstream runs as needed to supply it. This guide covers the common schedules and how to set them up. See [Orchestration](../concepts/orchestration.md) for how triggers work, and the [Playground](https://playground.duckstring.com) to try them in the browser before setting them on a Catchment.

## Where to put triggers

Put triggers on Outlets, the Ponds whose data is actually consumed. A trigger on a Pond in the middle of a pipeline works, but it keeps that Pond fresh whether or not anything downstream needs it, and it's easy to forget about.

When several Outlets share upstream Ponds, give each its own trigger. The shared Ponds run often enough to serve the most demanding Outlet, and each Outlet's own path runs at its own pace.

## A daily job

A Tide keeps a Pond no older than a limit. With a limit of a day, the pipeline runs once a day:

```bash
duckstring trigger tide reports 1d
```

If `reports` hasn't run in the last day, the first run starts as soon as you set the Tide. Each later run is due a day after the one before, so a Tide first run at 06:00 keeps running at about 06:00. If a run takes longer than the limit, the next starts as soon as it can, without runs piling up.

To tie runs to the clock more firmly, for example because the input lands at a fixed time, use a [window](#batch-sources) on the Inlet instead.

## As fresh as possible

A Wave keeps a Pond as current as the pipeline allows, starting a new run whenever the last one finishes:

```bash
duckstring trigger wave reports
```

Each Pond runs only as often as the slowest step in the pipeline can use its output, and a Pond whose inputs haven't changed skips its run. A Wave over a pipeline whose data rarely changes is cheap: only the Inlets keep checking for new data.

## Refresh on request

A one-off refresh, for example after fixing bad data upstream:

```bash
duckstring trigger pulse reports
```

A Pulse runs everything upstream that's older than the moment you sent it, then the target. A Tap is lighter: it asks for any newer data, and runs upstream Ponds only if nothing newer is already available.

To refresh from another system, such as when a user opens a dashboard, send a Tap over the HTTP API with a key at the demand level or higher:

```bash
curl -X POST -H "Authorization: Bearer $DEMAND_KEY" http://catchment.internal:7474/api/ponds/reports/tap
```

## Batch sources

Many sources update at known times: a warehouse export that lands overnight, a file drop at the end of each hour. Running an Inlet between updates wastes work, since there's nothing new to read. A window declares when the Inlet has new data:

```bash
# The export lands between 02:00 and 03:00 UTC every day
duckstring trigger window transactions add --name nightly --every 1d --start 02:00 --duration 1h
```

The Inlet then runs at most once per window, and not at all between windows. Its data is treated as fresh until the window closes, so downstream demand doesn't ask for more until the next one.

Combined with a Wave downstream, this runs the whole pipeline once a day, as soon as the new data is available:

```bash
duckstring trigger wave reports
```

The Wave keeps asking for newer data, the Inlet can't provide any until 02:00, and then the pipeline runs through once. No schedule has to be kept in step with the export.

Other window shapes:

```bash
# Weekdays only, every hour from 08:00, each window 10 minutes long
duckstring trigger window prices add --name trading --every 1h --start 08:00 --duration 10m --on MON,TUE,WED,THU,FRI

# Back-to-back 15-minute windows: at most one run per quarter hour
duckstring trigger window events add --name quarter --every 15m
```

Windows are set per Pond on the Catchment, and survive redeploys. List and remove them with `duckstring trigger window POND list` and `remove NAME`.

## Different cadences on one pipeline

In the demo pipeline, suppose `reports` is needed daily, and a second Outlet, `live_sales`, also reads `sales` and should be as current as possible:

```bash
duckstring trigger tide reports 1d
duckstring trigger wave live_sales
```

`sales` and its Inlets run as often as `live_sales` needs, and `reports` runs once a day from whatever `sales` has. If `live_sales` is later removed, `sales` drops to once a day with no change to anything else.

## Stopping

```bash
duckstring trigger remove reports    # remove a Wave or Tide; runs in progress finish
duckstring control sleep reports     # also clear any outstanding demand
```

`duckstring status` shows each Pond's standing trigger.
