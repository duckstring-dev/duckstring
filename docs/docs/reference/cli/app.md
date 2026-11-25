---
title: duckstring app
description: An App's instances, their output, scaling, and its data across major versions.
sidebar_label: app
---

# duckstring app

An App is a Pond declared with `type = "app"` that serves HTTP (see [`[app]`](../pond_toml.md#app)). It is deployed with [`pond deploy`](pond.md), refreshed with [`refresh`](refresh.md) and controlled with [`control`](control.md) like any Pond. These commands show its instances (each is one run of its process, from start to stop), restart and scale it, and manage its [Records and Media](../../guides/records_and_media.md) across major versions.

Every command takes `-c` / `--catchment`. Those acting on one major line also take `--major` / `-m` and `--version` / `-v`.

## `instances`

```bash
duckstring app instances NAME [--limit 20]
```

Lists the App's recent instances, newest first: where each ran (on the Catchment or elsewhere), when it started, became ready and stopped, and why it stopped, such as `idle`, `drained`, `redeploy`, `restart`, `killed`, `removed`, `shutdown`, `exited` or `start_failed`. An instance started by scaling says why. Needs read access.

## `logs`

```bash
duckstring app logs NAME [--instance ID] [--follow] [--tail BYTES]
```

Prints an instance's output, stdout and stderr together, for the newest instance unless `--instance` names one. `--follow` keeps printing new output until the instance stops. Needs full access, since an App's output can contain anything its code prints.

## `restart`

```bash
duckstring app restart NAME
```

Stops the App's instances and starts a fresh one. A failed or killed App is cleared first, so `restart` is also how to bring one back. It returns once the restart has begun; `app instances` shows the new instance once it is ready. Needs full access.

## `scale`

```bash
duckstring app scale NAME [--min N] [--max N] [--concurrency N] [--connections N]
                          [--requests F] [--cpu F] [--memory F] [--load F]
                          [--to N [--hold DURATION]] [--release] [--clear]
```

With no options, shows the App's scaling settings and where each comes from (`pond.toml`, an override or the default), its running instances with their active work, CPU, memory and reported load, and its recent scaling decisions. Needs read access. See [Scaling Apps](../../guides/scaling_apps.md).

The settings options override the App's [`[app]`](../pond_toml.md#app) and [`[app.scale]`](../pond_toml.md#appscale) values on the Catchment, and are kept across redeploys:

| Option | Overrides |
|---|---|
| `--min` | `min_instances`: instances kept running with no requests. |
| `--max` | `instances`: the most instances the App runs. |
| `--concurrency` | `concurrency`: the requests and active WebSockets one instance takes. |
| `--connections` | `connections`: the PostgreSQL connections one instance may hold. |
| `--requests`, `--cpu`, `--memory`, `--load` | The growth thresholds, as shares from above 0 to 1. |

`--clear` drops every override, back to `pond.toml`.

`--to N` sets the number of instances now, within `--min` and `--max`. Once that many are ready, the Catchment scales by load again. With `--hold`, such as `--hold 2h`, it keeps exactly that many for that long, idle or not. `--release` drops a count set with `--to` early.

Changing anything needs full access.

## `drain`

```bash
duckstring app drain NAME INSTANCE
```

Sends no new sessions to one instance, numbered as in `app scale` or `app instances`. Its requests in flight finish, and it stops once its work is done, or after 10 minutes if WebSockets stay open. A drained instance doesn't count towards `min_instances`, so another starts in its place when needed. Needs full access.

## `migrate`

```bash
duckstring app migrate NAME --major N
```

Migrates a trial major version again from the live one: its Records and Media are copied afresh from the live line, discarding what was written to the trial, and its `[app] migrate` command runs over the copy. [`pond promote`](pond.md) runs the final migration and makes the trial live. See [New major versions](../../guides/records_and_media.md#new-major-versions). Needs full access.

## `purge`

```bash
duckstring app purge NAME@MAJOR [--yes]
```

Deletes an App line's Records and Media for good: a retired line, a trial, or a line whose App was removed, since [`pond remove`](pond.md) keeps them. The live line of a deployed App can't be purged. Asks for confirmation unless `--yes` is given. Needs full access.
