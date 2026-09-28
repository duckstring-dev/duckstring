---
title: duckstring spout
description: Deliver a Pond's tables to external systems.
sidebar_label: spout
---

# duckstring spout

A Spout delivers a Pond's published tables to an external destination, such as an object store or a Postgres database, whenever the Pond publishes new output. Spouts are Catchment configuration and survive redeploys.

A Spout never asks its Pond to run; it delivers whatever the Pond publishes. Keep the Pond fresh with a trigger, and limit how often a Spout delivers with a [window](trigger.md#window) on `{pond}#{spout}`. Each Spout has its own run history and failure state, visible in `status` and the UI. A failed delivery is retried on the Pond's next 3 publishes; change the number with [`control failure-budget`](control.md#failure-budget) on `{pond}#{spout}`.

Every command takes `-c` / `--catchment`, `-m` / `--major` and `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `add`

```bash
duckstring spout add POND --to URI [--table TABLE | --all] [--mode MODE] [--name NAME]
```

| Option | Default | Description |
|---|---|---|
| `--to`, `-t` | required | The destination URI. Its scheme picks how data is written: `file://`, `s3://`, `gs://` or `postgres://`. Credentials are written as `${env:NAME}` or `${secret:NAME}` references and resolved only at delivery, and a single reference can be the whole destination. See [Destination URIs](../formats.md#destination-uris). |
| `--table`, `-T` | all tables | Deliver only this table. |
| `--all` | | Deliver every table (the default, stated explicitly). |
| `--mode` | `auto` | `auto`: send only changes where the destination supports it, otherwise the whole table. `full`: always write the whole table. `append`: for an object store, copy each run's new files, keeping the destination in Duckstring's file layout. |
| `--name`, `-n` | the table name, or the scheme for all tables | The Spout's name. A suffix such as `-2` is added if it's taken. |

Postgres delivery is incremental and exactly-once: each run deletes and re-inserts the changed rows in one transaction, and records its progress in a `_duckstring_egress` table in the destination. It needs a table with a primary key, which means a merge Trickle.

The destination is validated when the Spout is added, but credentials aren't checked until the first delivery. Use the UI's **Test** button to check them beforehand.

## `ls`

```bash
duckstring spout ls POND
```

Lists the Pond's Spouts with their destinations (credential references, never values), modes and states.

## `rm`

```bash
duckstring spout rm POND NAME
```

Removes a Spout. Data already delivered stays in the destination.

## Control

```bash
duckstring spout wake|force|sleep|kill|clear|resync POND NAME
```

| Command | Effect |
|---|---|
| `wake` | Resume delivering after a `sleep` or `kill`; the next delivery happens when the Pond next publishes. |
| `force` | Resume and deliver the current output again now. |
| `sleep` | Stop delivering new output. |
| `kill` | Stop delivering and hold the Spout until it's woken, forced or cleared. |
| `clear` | Reset a failed or killed Spout. |
| `resync` | Forget what has been delivered and deliver everything again in full. |
