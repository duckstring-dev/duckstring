---
title: duckstring app
description: An App's instances and their output.
sidebar_label: app
---

# duckstring app

An App is a Pond declared with `type = "app"` that serves HTTP (see [`[app]`](../pond_toml.md#app)). It is deployed with [`pond deploy`](pond.md), refreshed with [`refresh`](refresh.md) and controlled with [`control`](control.md) like any Pond. These commands show its instances: each is one run of its process, from start to stop.

Both commands take `-c` / `--catchment`, `--major` / `-m` and `--version` / `-v`.

## `instances`

```bash
duckstring app instances NAME [--limit 20]
```

Lists the App's recent instances, newest first: when each started, became ready and stopped, and why it stopped (`idle`, `redeploy`, `killed`, `removed`, `shutdown`, `exited` or `start_failed`). Needs read access.

## `logs`

```bash
duckstring app logs NAME [--instance ID] [--follow] [--tail BYTES]
```

Prints an instance's output, stdout and stderr together, for the newest instance unless `--instance` names one. `--follow` keeps printing new output until the instance stops. Needs full access, since an App's output can contain anything its code prints.
