---
title: duckstring refresh
description: Refresh a Pond now, every N, at set times, continuously, or when it's queried.
sidebar_label: refresh
---

# duckstring refresh

Commands that ask for a Pond's data to be refreshed: now, on a recurring basis, or whenever an app queries it through a Tap. See [Orchestration](../../concepts/orchestration.md#refreshes) for how a Refresh behaves. Refreshes are meant to be placed on Outlets, though any deployed Pond accepts them.

Every command takes `-c` / `--catchment`, `-m` / `--major` and `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `refresh now`

```bash
duckstring refresh now POND [--silent] [--watch]
```

Asks for data at least as fresh as the moment the command runs. Every Pond upstream that is older runs (an optional Source only if it has never run), and the result flows down to `POND`. Requests that arrive faster than the pipeline can serve them are paced to its slowest step. Opens the live status view until the Pond settles; `--watch` keeps it open, and `--silent` returns once the request is sent.

## `refresh add`

```bash
duckstring refresh add POND --every PERIOD [--start TIME [--on DAYS] [--until TIME]] [--name NAME] [--silent]
duckstring refresh add POND --continuous [--name NAME] [--silent]
```

Adds a recurring Refresh, which repeats the request:

- **Every** (`--every` alone): asks once `PERIOD` has passed since the later of the last request and the last refresh of the data, so any other refresh restarts the count. `duckstring refresh add reports --every 1d` refreshes `reports` daily, and the data is at most a day plus the pipeline's run time old.
- **At set times** (`--every` with `--start`): asks at each time on the grid `START + k × PERIOD`, whatever else refreshes the Pond, and the data is labelled with that time. A Catchment that was down through some of them asks once, for the latest, when it returns.
- **Continuous** (`--continuous`, or `--every 0s`): asks whenever the Pond can take more, paced to the pipeline's slowest step.

A Pond can have any number of scheduled Refreshes and one `--every` or continuous Refresh. Adding another `--every` or continuous Refresh replaces it; adding one with an existing name replaces that one. Opens the live status view (a recurring Refresh keeps it open until `Ctrl+C`) unless `--silent`.

| Option | Default | Description |
|---|---|---|
| `--every`, `-e` | required unless `--continuous` | The period, a [duration](../formats.md#durations) such as `30s`, `30m`, `12h`, `1d` or `1h30m`. |
| `--continuous` | | As fresh as the pipeline allows. The same as `--every 0s`. |
| `--start`, `-s` | none | Pin the Refresh to a schedule from this time: ISO 8601, or `HH:MM` for today in UTC. Needs a period. |
| `--on`, `-o` | every day | With `--start`: only on these weekdays, such as `MON,TUE,WED,THU,FRI`. |
| `--until`, `-u` | none | With `--start`: when the schedule ends, in ISO 8601. |
| `--name`, `-n` | described from the options | A name, unique on this Pond, such as `Daily at 06:00`, `Weekdays at 06:00`, `Every 30 min` or `Continuous`. |

```bash
duckstring refresh add reports --every 1d --start 06:00 --on MON,TUE,WED,THU,FRI
```

## `refresh ls`

```bash
duckstring refresh ls POND
```

Lists the Pond's recurring Refreshes, scheduled first, with their details and when each next asks.

## `refresh rm`

```bash
duckstring refresh rm POND NAME
```

Removes a recurring Refresh by name. Runs already in progress finish. `duckstring control sleep` removes them all and clears outstanding demand.


## `refresh tap`

A Tap refreshes a Pond whenever an app queries it. It has its own key, which can query only the Tap's tables, and a freshness policy: a query through the Tap sends the Pond a Refresh when the newest request on the Pond, or with none waiting its freshness, is at least `--fresh-within` old. Queries with any other key never send one. See [Serving Apps with Taps](../../guides/serving_apps_with_taps.md).

A Tap belongs to one major version of its Pond. Taps are Catchment configuration and survive redeploys; removing the major version removes them. Managing Taps needs a full-access key.

### `refresh tap add`

```bash
duckstring refresh tap add POND --name NAME [--fresh-within DURATION] [--wait DURATION] [--tables T1,T2]
```

Adds a Tap and prints its key. The key is shown once; only a hash of it is kept.

| Option | Default | Description |
|---|---|---|
| `--name`, `-n` | required | The Tap's name: letters, digits, `.`, `_` and `-`, up to 64 characters. |
| `--fresh-within`, `-f` | `0` | Send a Refresh on a query when the newest request on the Pond, or the data, is at least this old. `0` refreshes on every query, except while a request is already waiting for the Pond to start a run. |
| `--wait`, `-w` | none | Hold a query up to this long until the data is fresh within `--fresh-within`, then answer it with what has been published. Without it a query is answered at once. A query to a failed or blocked Pond is never held. |
| `--tables`, `-t` | all exposed tables | Comma-separated tables the Tap can read. A Tap only reads tables its Pond exposes to read-only users (see [`serve expose`](serve.md#expose)). |

Durations combine units: `30s`, `5m`, `1h30m`, `1d`.

### `refresh tap ls`

```bash
duckstring refresh tap ls POND
```

Lists the Pond's Taps with their tables, policy, when each was last queried, and how many queries and Refreshes each has had. Keys are never shown.

### `refresh tap rotate`

```bash
duckstring refresh tap rotate POND NAME
```

Prints a new key for the Tap. The old key stops working at once.

### `refresh tap rm`

```bash
duckstring refresh tap rm POND NAME
```

Removes the Tap. Its key stops working at once, and runs it already caused finish.
