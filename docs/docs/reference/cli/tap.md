---
title: duckstring tap
description: Give apps an access point that keeps a Pond fresh while it's used.
sidebar_label: tap
---

# duckstring tap

A Tap is an access point on a Pond for one app. It has its own key, which can query only the Tap's tables, and a freshness policy: a query through the Tap sends the Pond a Refresh when the newest request on the Pond, or with none waiting its freshness, is at least `--fresh-within` old. Queries with any other key never send one. See [Serving Apps with Taps](../../guides/serving_apps_with_taps.md).

A Tap belongs to one major version of its Pond. Taps are Catchment configuration and survive redeploys; removing the major version removes them. Managing Taps needs a full-access key.

Every command takes `-c` / `--catchment`, `-m` / `--major` and `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `add`

```bash
duckstring tap add POND --name NAME [--fresh-within DURATION] [--wait DURATION] [--tables T1,T2]
```

Adds a Tap and prints its key. The key is shown once; only a hash of it is kept.

| Option | Default | Description |
|---|---|---|
| `--name`, `-n` | required | The Tap's name: letters, digits, `.`, `_` and `-`, up to 64 characters. |
| `--fresh-within`, `-f` | `0` | Send a Refresh on a query when the newest request on the Pond, or the data, is at least this old. `0` refreshes on every query, except while a request is already waiting for the Pond to start a run. |
| `--wait`, `-w` | none | Hold a query up to this long until the data is fresh within `--fresh-within`, then answer it with what has been published. Without it a query is answered at once. A query to a failed or blocked Pond is never held. |
| `--tables`, `-t` | all exposed tables | Comma-separated tables the Tap can read. A Tap only reads tables its Pond exposes to read-only users (see [`serve expose`](serve.md#expose)); `add` warns when none of its tables is exposed. |

Durations combine units: `30s`, `5m`, `1h30m`, `1d`.

## `ls`

```bash
duckstring tap ls POND
```

Lists the Pond's Taps with their tables, policy, when each was last queried, and how many queries and Refreshes each has had. Keys are never shown.

## `rotate`

```bash
duckstring tap rotate POND NAME
```

Prints a new key for the Tap. The old key stops working at once.

## `rm`

```bash
duckstring tap rm POND NAME
```

Removes the Tap. Its key stops working at once, and runs it already caused finish.
