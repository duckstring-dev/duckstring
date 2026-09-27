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

| Group | Purpose |
|---|---|
| [`catchment`](catchment.md) | Create, register, start and manage Catchments; ducts between them. |
| [`pond`](pond.md) | Scaffold, test locally, deploy and remove Ponds. |
| [`puddle`](puddle.md) | Inspect local Puddles and run output. |
| [`trigger`](trigger.md) | Tap, Wave, Pulse, Tide, and windows. |
| [`control`](control.md) | Wake, force, refresh, repair, sleep, kill, clear, reset, and retry budgets. |
| [`do`](do.md) | Apply operations to many Ponds at once. |
| [Data and status](data.md) | `status`, `query`, `get`, `objects`, `get-object`, `delete-table`, `delete-object`, `lineage`, `trace`. |
| [`duck`](duck.md) | Per-Pond compute and Duck pools. |
| [`spout`](spout.md) | Deliver tables to external systems. |
| [`serve`](serve.md) | The catalog: query, expose and promote. |
| [`secret`](secret.md) | The Catchment's write-only secret store. |
| [`alert`](alert.md) | Failure and freshness notifications. |

## Choosing a Catchment

Commands that talk to a Catchment take `--catchment` / `-c` with a registered name. Without it, they use the default Catchment, or the only registered one if there is exactly one. Registrations live in `~/.duckstring/config.toml`, which is created with `0600` permissions since it may hold API keys.

## Choosing a major version

Commands that act on one Pond take:

| Option | Description |
|---|---|
| `--major`, `-m` | The major version line to act on. Defaults to the highest deployed major. |
| `--version`, `-v` | A full version such as `1.2.0`. Selects its major line, and must be that line's currently deployed version, otherwise the command fails. |

[`do`](do.md) also accepts a Pond as `name@major`.

## Live status

`trigger tap`, `trigger pulse`, `control wake` and `control force` open the live status view after sending the request, and close it once the Pond settles (idle, failed, killed or blocked). `trigger wave` and `trigger tide` open it and leave it open until you press `Ctrl+C`; the trigger keeps running after the view closes.

| Option | Description |
|---|---|
| `--silent` | Send the request without opening the status view. |
| `--watch` | Keep the view open after the Pond settles (one-shot commands only). |

## Access levels

Against a Catchment using built-in API keys, each command needs a minimum key level. Read commands (status, queries, lineage) need `read`; triggers need `demand`; everything that changes configuration, runs or data needs `full`. See [HTTP API](../http_api.md#authentication).
