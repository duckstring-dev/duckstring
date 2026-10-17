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

## Live status

`refresh now`, `control wake` and `control force` open the live status view after sending the request, and close it once the Pond settles (idle, failed, killed or blocked). `refresh add` opens it and leaves it open until you press `Ctrl+C`; the recurring Refresh keeps going after the view closes.

| Option | Description |
|---|---|
| `--silent` | Send the request without opening the status view. |
| `--watch` | Keep the view open after the Pond settles (one-shot commands only). |

## Access levels

Against a Catchment using built-in API keys, each command needs a minimum key level. Read commands (status, queries, lineage) need `read`; Refreshes need `demand`; everything that changes configuration, runs or data needs `full`. See [HTTP API](../http_api.md#authentication).
