---
title: duckstring trigger
description: Tap, Wave, Pulse, Tide and windows.
sidebar_label: trigger
---

# duckstring trigger

Commands that create demand on a Pond. See [Orchestration](../../concepts/orchestration.md#triggers) for how each trigger behaves. Triggers are meant to be placed on Outlets, though any deployed Pond accepts them.

Every command takes `-c` / `--catchment`, `-m` / `--major` and `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version). A Pond holds at most one standing trigger: setting a Wave or Tide replaces the existing one.

## `tap`

```bash
duckstring trigger tap POND [--silent] [--watch]
```

Asks the Pond to run once with fresher data than it has. If its Sources have nothing newer, the request passes upstream until it reaches Ponds that can run. Opens the live status view until the Pond settles.

## `wave`

```bash
duckstring trigger wave POND [--silent]
```

A standing Tap: repeats whenever the Pond finishes a run, so the pipeline runs as often as its slowest step allows. Stays in place until removed.

## `pulse`

```bash
duckstring trigger pulse POND [--silent] [--watch]
```

Asks for data at least as fresh as the moment the command runs. Every Pond upstream that is older runs, and the result flows down to `POND`. Opens the live status view until the Pond settles.

## `tide`

```bash
duckstring trigger tide POND BOUND [--silent]
```

A standing Pulse that keeps `POND` no older than `BOUND`. In effect it runs the pipeline every `BOUND`: `duckstring trigger tide reports 1d` refreshes `reports` daily. Precisely, it sends a Pulse whenever the data would otherwise become older than `BOUND`. If the pipeline takes longer than `BOUND`, it runs back to back without piling up requests.

| Argument | Description |
|---|---|
| `BOUND` | A [duration](../formats.md#durations), such as `30s`, `12h`, `1d` or `1h30m`. |

## `remove`

```bash
duckstring trigger remove POND
```

Removes the Pond's standing Wave or Tide. Runs already in progress finish.

## `window`

```bash
duckstring trigger window POND add --name NAME --every INTERVAL [options]
duckstring trigger window POND list
duckstring trigger window POND remove NAME
```

Windows declare when an Inlet can produce new data. The Pond runs at most once per window, never between windows, and its data counts as fresh until the window ends. See [Windows](../../concepts/orchestration.md#windows).

Windows are Catchment configuration and survive redeploys. A window can also be set on a Spout, as `{pond}#{spout}`, to limit how often it delivers.

### `add` options

| Option | Default | Description |
|---|---|---|
| `--name`, `-n` | required | A name for the window, unique on this Pond. |
| `--every`, `-e` | required | How often windows open, as a single-unit [duration](../formats.md#durations) such as `1d`, `12h` or `10s`. |
| `--start`, `-s` | 00:00 today (UTC) | When the first window opens: ISO 8601, or `HH:MM` for today in UTC. |
| `--duration`, `-d` | `--every` | How long each window stays open. Omitting it gives back-to-back windows. |
| `--on`, `-o` | every day | Only open on these weekdays, such as `MON,WED,FRI`. |
| `--until`, `-u` | none | When the windows stop, in ISO 8601. |

Windows on one Pond can't overlap; an overlapping window is rejected.

```bash
# A nightly export that lands between 02:00 and 03:00 UTC
duckstring trigger window transactions add --name nightly --every 1d --start 02:00 --duration 1h
```
