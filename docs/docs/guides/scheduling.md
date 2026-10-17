---
title: Scheduling
description: Choose Refreshes and windows for common schedules.
---

# Scheduling

Duckstring schedules from the end of the pipeline: you put a Refresh on the Pond whose data someone uses, and everything upstream runs as needed to supply it. This guide covers the common schedules and how to set them up. See [Orchestration](../concepts/orchestration.md) for how Refreshes work, and the [Playground](https://playground.duckstring.com) to try them in the browser before setting them on a Catchment.

## Where to put Refreshes

Put Refreshes on Outlets, the Ponds whose data is actually consumed. A Refresh on a Pond in the middle of a pipeline works, but it keeps that Pond fresh whether or not anything downstream needs it, and it's easy to forget about.

When several Outlets share upstream Ponds, give each its own Refresh. The shared Ponds run often enough to serve the most demanding Outlet, and each Outlet's own path runs at its own pace.

## Refreshing now

A one-off refresh, for example after fixing bad data upstream:

```bash
duckstring refresh now reports
```

Everything upstream that's older than the moment you sent it runs, then `reports`, and the live status view stays open until it settles. Add `--silent` to send the request and return.

To refresh from another system, send the same request over the HTTP API with a key at the demand level or higher:

```bash
curl -X POST -H "Authorization: Bearer $DEMAND_KEY" http://catchment.internal:7474/api/ponds/reports/refresh
```

## Every day, or every N

A recurring Refresh repeats the request. With a period of a day, the pipeline runs once a day:

```bash
duckstring refresh add reports --every 1d
```

The day counts from the later of the last request and the last refresh of the data. If `reports` hasn't been refreshed in the last day, the first request goes as soon as you add it, and each later one is due a day after the one before, so a Refresh first sent at 06:00 keeps asking at about 06:00. Any other refresh of `reports`, a manual one or a Sink's demand, moves it on, so it never adds a run the data didn't need. The data is at most a day plus the pipeline's run time old. If a run takes longer than the period, the next starts as soon as it can, without runs piling up.

Periods combine units, such as `30m`, `12h` or `1h30m`.

## At set times

When the time of day matters, for example because people read the report at 08:00, pin the Refresh to a schedule with `--start`:

```bash
duckstring refresh add reports --every 1d --start 06:00
duckstring refresh add reports --every 1d --start 06:00 --on MON,TUE,WED,THU,FRI --name weekday-morning
```

A scheduled Refresh asks at each time on its grid, here 06:00 every day (or every weekday), whatever else has refreshed the Pond, and the data is labelled with that time. `--start` takes `HH:MM` in UTC or an ISO 8601 time, `--on` limits it to some weekdays, and `--until` ends it. If the Catchment is down at 06:00, it asks once when it comes back.

A Pond can have several scheduled Refreshes, for example one at 06:00 and one at 13:00, beside one `--every` or continuous Refresh. Adding another `--every` or continuous Refresh replaces the one it has. Give a Refresh a name with `--name`, or let Duckstring name it ("Daily at 06:00").

## As fresh as possible

A continuous Refresh keeps a Pond as current as the pipeline allows:

```bash
duckstring refresh add reports --continuous
```

Duckstring paces the requests to the slowest step, so each Pond runs only as often as that step can use its output, and a Pond whose inputs haven't changed skips its run. A continuous Refresh over a pipeline whose data rarely changes is cheap: only the Inlets keep checking for new data. `--every 0s` is the same thing.

A continuous Refresh doesn't always give the freshest data on average. The pacing starts the Ponds before the slowest step as soon as it starts a run, so their output can wait for it to finish, adding up to one run of that step to the data's age. Where that matters, a Refresh every N a little longer than the slowest step can be fresher on average for less compute, depending on how the run times before the slowest step line up with it. The [Playground](https://playground.duckstring.com)'s run charts show the data's age under each.

## Only while it's used

Some data only matters while someone is looking at it, such as a dashboard opened a few times a day. Rather than a recurring Refresh, give the app a [Tap](serving_apps_with_taps.md): its queries refresh the Pond when the data is older than you allow, and nothing runs while the app is idle.

```bash
duckstring refresh tap add reports --name dashboard --fresh-within 1h
```

## Batch sources

Many sources update at known times: a warehouse export that lands overnight, a file drop at the end of each hour. Running an Inlet between updates wastes work, since there's nothing new to read. A window declares when the Inlet has new data:

```bash
# The export lands between 02:00 and 03:00 UTC every day
duckstring window add transactions --name nightly --every 1d --start 02:00 --duration 1h
```

The Inlet then runs at most once per window, and not at all between windows. Its data is treated as fresh until the window closes, so downstream demand doesn't ask for more until the next one.

Combined with a continuous Refresh downstream, this runs the whole pipeline once a day, as soon as the new data is available:

```bash
duckstring refresh add reports --continuous
```

The Refresh keeps asking for newer data, the Inlet can't provide any until 02:00, and then the pipeline runs through once. No schedule has to be kept in step with the export.

Other window shapes:

```bash
# Weekdays only, every hour from 08:00, each window 10 minutes long
duckstring window add prices --name trading --every 1h --start 08:00 --duration 10m --on MON,TUE,WED,THU,FRI

# Back-to-back 15-minute windows: at most one run per quarter hour
duckstring window add events --name quarter --every 15m
```

Windows are set per Pond on the Catchment, and survive redeploys. List and remove them with `duckstring window ls POND` and `duckstring window rm POND NAME`.

## Different cadences on one pipeline

In the demo pipeline, suppose `reports` is needed daily, and a second Outlet, `live_sales`, also reads `sales` and should be as current as possible:

```bash
duckstring refresh add reports --every 1d
duckstring refresh add live_sales --continuous
```

`sales` and its Inlets run as often as `live_sales` needs, and `reports` runs once a day on whatever `sales` has. If `live_sales` is later removed, `sales` drops to once a day with no change to anything else.

## Optional Sources

A Source marked optional in `pond.toml` is read but never waited for. Suppose `sales` took `products` as optional. A Refresh on `reports` builds `products` once, so `sales` has something to read, but doesn't ask for it again after that, since `sales` would never wait for it. To keep an optional Source current, give it its own Refresh:

```bash
duckstring refresh add products --every 1d
```

This is the one case where a Refresh belongs on a Pond in the middle of a pipeline.

## Stopping

```bash
duckstring refresh ls reports                 # the recurring Refreshes, and when each next asks
duckstring refresh rm reports "Daily at 06:00"  # remove one; runs in progress finish
duckstring control sleep reports              # remove them all and clear any outstanding demand
```

`duckstring status` shows each Pond's recurring Refreshes.
