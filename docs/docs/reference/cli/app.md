---
title: duckstring app
description: An App's instances, their output, and restarting it.
sidebar_label: app
---

# duckstring app

An App is a Pond declared with `type = "app"` that serves HTTP (see [`[app]`](../pond_toml.md#app)). It is deployed with [`pond deploy`](pond.md), refreshed with [`refresh`](refresh.md) and controlled with [`control`](control.md) like any Pond. These commands show its instances (each is one run of its process, from start to stop) and restart it.

Every command takes `-c` / `--catchment`, `--major` / `-m` and `--version` / `-v`.

## `instances`

```bash
duckstring app instances NAME [--limit 20]
```

Lists the App's recent instances, newest first: when each started, became ready and stopped, and why it stopped (`idle`, `redeploy`, `restart`, `killed`, `removed`, `shutdown`, `exited` or `start_failed`). Needs read access.

## `logs`

```bash
duckstring app logs NAME [--instance ID] [--follow] [--tail BYTES]
```

Prints an instance's output, stdout and stderr together, for the newest instance unless `--instance` names one. `--follow` keeps printing new output until the instance stops. Needs full access, since an App's output can contain anything its code prints.

## `restart`

```bash
duckstring app restart NAME
```

Stops the App's instance and starts a fresh one. A failed or killed App is cleared first, so `restart` is also how to bring one back. It returns once the restart has begun; `app instances` shows the new instance once it is ready. Needs full access.
