---
title: duckstring do
description: Apply operations to many Ponds at once.
sidebar_label: do
---

# duckstring do

```bash
duckstring do [POND...] [selection options] [operations] [--confirm NAME] [--yes] [-c NAME]
```

Applies one or more operations to a set of Ponds. It is the command-line equivalent of **Options → Pond Actions** in the web UI.

## Selecting Ponds

Name Ponds as `name` (the highest deployed major) or `name@major`, then optionally widen the selection:

| Option | Adds |
|---|---|
| `--all` | Every Pond in the Catchment. |
| `--tree` | Every Pond connected to the selection. |
| `--between` | Every Pond on a path between two selected Ponds. |
| `--downstream` | Everything downstream of the selection. |

## Operations

Operations are applied in this order, whatever order they're given in:

| Order | Option | Effect |
|---|---|---|
| 1 | `--kill` | Stop the Duck and hold the Pond killed. |
| 2 | `--sleep` | Clear demand; runs in progress finish. |
| 3 | `--reset` | Delete all tables and Objects and reset freshness. |
| 4 | `--wipe` | Delete run history, leaving data alone. |
| 5 | `--remove` | Retire the major line: data, configuration and attached Spouts and alerts. Includes `--reset`. |
| 6 | `--clear` | Clear a failure and unblock downstream. |
| 7 | `--repair` | Rebuild the selection now, in dependency order. |
| 8 | `--refresh` | Rebuild from scratch on the next run. |

Each operation behaves like the matching [`control`](control.md) or [`pond remove`](pond.md#remove) command. `--repair` can't be combined with `--remove` or `--reset`. Once a Pond is removed, later operations skip it.

## Confirmation

`--reset`, `--wipe` and `--remove` can't be undone, so they need the Catchment's name, given with `--confirm NAME` or typed at a prompt. `--yes` suppresses the prompt, so a script using these operations must pass `--confirm`.

An error on one Pond is reported and doesn't stop the others.

```bash
# Rebuild sales and everything that depends on it
duckstring do sales --downstream --repair

# Retire every Pond in the Catchment
duckstring do --all --sleep --remove --confirm dev
```
