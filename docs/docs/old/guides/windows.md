---
title: Windows
description: Batch availability on Inlets — when external data is actually fresh.
---

# Windows

An [Inlet](../concepts/ponds.md) consumes data Duckstring doesn't control — a warehouse export that lands nightly, an API refreshed hourly, a file drop on weekday mornings. A **Window** tells the Catchment when that external data is actually available, so the freshness model can reason about it instead of guessing.

## What a Window does

A Window is a recurring period during which the Inlet may start, with one crucial addition: data ingested in a window is considered **fresh until the window's end**. An Inlet with a daily window that ran this morning *stays fresh all day* — downstream Ponds holding pull demand won't re-run against it, because no newer source data can exist yet. When the next window opens, the Inlet's output is stale by definition, and demand flows again.

Between occurrences the Inlet simply doesn't start; demand waits for the window to open rather than being lost.

## Adding windows

Windows are managed per-Pond from the CLI (or the [web UI](web-ui.md)):

```bash
duckstring trigger window transactions add --name nightly --every 1d --start 02:00 --duration 3h
duckstring trigger window transactions list
duckstring trigger window transactions remove nightly
```

The recurrence is RFC-5545-flavoured — anchored occurrences at a fixed interval, optionally filtered and bounded. There is no cron anywhere:

| Option | Default | Meaning |
|---|---|---|
| `--name`, `-n` | *(required)* | Identifier for the rule |
| `--every`, `-e` | *(required)* | Interval between occurrences — single unit: `10s`, `12h`, `1d`, `1w` |
| `--start`, `-s` | `00:00` today | Anchor for the first occurrence (ISO 8601 or `HH:MM`, UTC) |
| `--duration`, `-d` | = `--every` | How long each window stays open (compound ok: `1h30m`) |
| `--on`, `-o` | all days | Restrict to weekdays: `MON,WED,FRI` |
| `--until`, `-u` | never | Expiry (ISO 8601) |

A Pond can hold several named windows (e.g. a weekday-morning rule plus a weekend rule); overlapping rules are rejected at add time. Omitting `--duration` makes occurrences back-to-back — the window is always open, and its only effect is the freshness granularity.

Windows are **operational config**, owned by the Catchment: they're set by operators against the live Pond, never declared in `pond.toml`, and they survive redeploys.

## The signature pattern: Window + Wave

The pair that replaces a scheduler entirely:

```bash
duckstring trigger window transactions add --name nightly --every 1d --start 02:00 --duration 3h
duckstring trigger wave reports
```

The Wave keeps standing pull on the whole lineage; the Window throttles it at the source. Result: the pipeline runs **exactly once per day, starting at 02:00** — when fresh data exists — and is idle the rest of the time. Compared to a 24-hour [Tide](triggers.md), the timing is *pinned to the source's actual update*, not to wherever the clock happened to land; and unlike cron, a delayed upstream export simply delays the run rather than feeding it yesterday's data.

The general law for mixed cadences: a pulled pipeline runs at the rate of its slowest constraint — the bottleneck Ripple or the longest Window interval, whichever is slower. An hourly-windowed Inlet and a daily-windowed Inlet feeding the same Sink give the Sink a daily cadence (it needs both to be fresher than its last run), while any Pond downstream of only the hourly Inlet still ticks hourly.

## "Do not consume" periods

The duration doubles as an exclusion mechanism. A source system that *writes* between 00:00 and 02:00 gets a window of `--start 02:00 --duration 22h`: the Inlet can start any time except during the nightly write, so a half-written export is never ingested.

## Mechanics

Under the hood the window plugs into the [freshness model](../concepts/freshness.md) as the Inlet's *effective source freshness*: the freshness an Inlet run acquires is the end of the active window (rather than the run's start time), which is what makes "fresh until the window closes" fall out of the ordinary `sourceF > startF` rule. The precise treatment — including staleness measurement under windows — is in [Theory](../theory.md#batch-updating-data-sources).
