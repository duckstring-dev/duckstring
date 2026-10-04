---
title: duckstring control
description: Act directly on a Pond's execution and health.
sidebar_label: control
---

# duckstring control

Commands that act directly on a Pond's execution, outside the demand model. See [Failures](../../concepts/management_and_execution.md#failures) and [Control](../../concepts/management_and_execution.md#control) for the concepts.

Every command takes `-c` / `--catchment`, and those acting on one Pond take `-m` / `--major` and `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## Running

### `wake`

```bash
duckstring control wake POND [--silent] [--watch]
```

Runs the Pond once if its Sources already have newer data, without asking them to run. Clears a failed or killed state first. Opens the live status view until the Pond settles.

### `force`

```bash
duckstring control force POND [--silent] [--watch]
```

Reruns the Pond now at its current freshness, even with no upstream change, for example after deploying a fix. Freshness doesn't change, so Ponds downstream don't rerun because of it. Clears a failed or killed state first.

### `refresh`

```bash
duckstring control refresh POND [--clear]
```

Marks the Pond so its next run rebuilds from scratch: its working database is dropped and every Source is read in full. Trickles restart their change history, so Ponds downstream also read them in full on their next run. Nothing runs immediately; the rebuild happens whenever the Pond next runs. `--clear` removes a pending mark.

### `repair`

```bash
duckstring control repair POND... [--downstream]
```

Rebuilds a set of Ponds now, in dependency order, each reading its parents' rebuilt output. The set must be connected: if two selected Ponds are linked through a third, the third must be selected too. `--downstream` adds everything downstream of the selection. While repairing, the Ponds don't respond to other demand.

## Stopping

### `sleep`

```bash
duckstring control sleep POND [--upstream]
```

Clears the Pond's demand and removes its standing trigger. Runs already in progress finish. `--upstream` also sleeps every Pond upstream of it.

### `kill`

```bash
duckstring control kill POND
```

Stops the Pond's Duck immediately, abandoning its current run, and holds the Pond in a killed state. A killed Pond doesn't run or retry until it's woken, forced or cleared.

## Failures

### `clear`

```bash
duckstring control clear POND
```

Resets a failed or killed Pond without running it, and unblocks the Ponds downstream of it.

### `failure-budget`

```bash
duckstring control failure-budget POND [--immediate N] [--on-change N]
```

Shows the Pond's retry budgets, or sets them. These values replace the ones from `pond.toml` and persist across redeploys.

It also works on a Spout, addressed as `{pond}#{spout}`. A Spout retries a failed delivery on its Pond's next 3 publishes by default; `--on-change` changes that number.

| Option | Description |
|---|---|
| `--immediate`, `-i` | How many times a failed Ripple is retried within the same Pond Run. |
| `--on-change`, `-o` | How many times a failed Pond Run is retried when a Source next updates. |

### `reset-contract`

```bash
duckstring control reset-contract POND [--yes]
```

Forgets the output schema recorded for the Pond's major line, so the next successful run records it again, and clears the failure. Use it when a run failed for narrowing a column's type and you intend to accept the change within the same major version. Ponds downstream may depend on the old schema, so it asks for confirmation.

## Resetting

### `reset`

```bash
duckstring control reset POND [--clear-history] [--yes]
```

Returns the Pond to its freshly deployed state: deletes its published data, working database and run ledger, and resets its freshness. Keeps its code, configuration and demand, so it rebuilds from scratch the next time it runs. The Pond must be idle.

| Option | Description |
|---|---|
| `--clear-history` | Also delete the Pond's run history. |
| `--yes`, `-y` | Skip the confirmation. |
