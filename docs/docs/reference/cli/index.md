---
title: CLI Overview
description: Conventions shared by every command.
sidebar_label: Overview
---

# CLI Overview

The CLI is installed as `duckstring`, with `ds` as a shorter alias. Every command prints its own help with `--help`.

```bash
duckstring --version              # print the installed version
duckstring --install-completion   # enable shell completion
```

| Command | Purpose |
|---|---|
| [`status`](data.md#status) | Every Pond's state, freshness and recurring Refreshes. |
| [`runs`](data.md#runs) | Recent Pond Runs and their Ripple Runs, with errors. |
| [`query`](data.md#query) | Read-only SQL across the catalog, or a table's first rows. |
| [`get`](data.md#get) | Download a table's files or an Object. |
| [`refresh`](refresh.md) | Refresh a Pond now, every N, at set times, continuously, or when it's queried (Taps). |
| [`window`](window.md) | When an Inlet can produce new data. |
| [`control`](control.md) | Wake, force, rebuild, repair, sleep, kill, clear, reset, wipe and retry budgets, for one Pond or many. |
| [`data`](data.md#data) | List, expose and delete tables and Objects, and their lineage. |
| [`pond`](pond.md) | Scaffold, test locally, deploy, promote and remove Ponds. |
| [`catchment`](catchment.md) | Create, register, start and manage Catchments; ducts between them. |
| [`duck`](duck.md) | Per-Pond compute and Duck pools. |
| [`spout`](spout.md) | Deliver tables to external systems. |
| [`secret`](secret.md) | The Catchment's write-only secret store. |
| [`alert`](alert.md) | Failure and freshness notifications. |
| [`app`](app.md) | An App's instances and their output, and restarting it. |

Tables and Objects are named `POND.NAME`, as in the Python API, for example `reports.monthly_summary`.

## Choosing a Catchment

Commands that talk to a Catchment take `--catchment` / `-c` with a registered name. Without it, they use the default Catchment, or the only registered one if there is exactly one. Registrations live in `~/.duckstring/config.toml`, which is created with `0600` permissions since it may hold API keys.

## Choosing a major version

Commands that act on one Pond take:

| Option | Description |
|---|---|
| `--major`, `-m` | The major version line to act on. Defaults to the highest deployed major. |
| `--version`, `-v` | A full version such as `1.2.0`. Selects its major line, and must be that line's currently deployed version, otherwise the command fails. |

[`control`](control.md#choosing-ponds) also accepts several Ponds, each as `name` or `name@major`.

## Following a request

`refresh now`, `control wake` and `control force` follow the request they send. In a terminal they open the live status view and close it once the Pond settles (idle, failed, killed or blocked); `refresh add` opens it and leaves it open until you press `Ctrl+C`, and the recurring Refresh keeps going after the view closes.

When the output isn't a terminal (a script, a pipe, an agent), the one-shot commands wait for the outcome instead, as with `--wait`, and `refresh add` and `status` print once and exit.

| Option | Description |
|---|---|
| `--silent` | Send the request and return. |
| `--watch` | Keep the view open after the Pond settles (one-shot commands only). |
| `--wait` | Wait until the Pond has done what was asked and settled, then exit with its outcome (below). |
| `--timeout` | With `--wait`: give up after this [duration](../formats.md#durations), such as `10m`. Without it, waiting has no limit. |
| `--live` | Open the live status view even when the output isn't a terminal. |

`control rebuild` and `pond deploy` also take `--wait` and `--timeout`. They change what the next Run does rather than starting one, so they wait for that Run: something has to ask for it, such as a recurring Refresh or a `refresh now`.

A command that waits exits with:

| Code | Meaning |
|---|---|
| `0` | The Pond reached what was asked, and every Run it caused, upstream included, succeeded. It prints the Pond's new freshness. |
| `1` | A Run it caused failed, or the Pond is failed, killed or blocked. It prints each failed Run's Pond, freshness, Ripple and error. |
| `2` | `--timeout` passed first. |

`duckstring runs POND --ripples` then shows the Runs in detail.

## Access levels

Against a Catchment using built-in API keys, each command needs a minimum key level. Read commands (status, queries, lineage) need `read`; Refreshes need `demand`; everything that changes configuration, runs or data needs `full`. See [HTTP API](../http_api.md#authentication).
