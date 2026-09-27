---
title: duckstring duck
description: Per-Pond compute and Duck pools.
sidebar_label: duck
---

# duckstring duck

Commands for where each Pond runs and what compute it gets. See [Cloud Compute](../../concepts/management_and_execution.md#cloud-compute) for the concepts.

A Pond's effective settings are, for each setting, the operator override set with `duck set` if there is one, otherwise the value declared in [`pond.toml`](../pond_toml.md), otherwise the Catchment's default. Remote compute only takes effect when cloud compute is enabled on the Catchment (see [`catchment settings`](catchment.md#settings)); otherwise every Pond runs on the Catchment's machine.

## `show`

```bash
duckstring duck show [POND] [-m N]
```

Shows a Pond's effective compute settings and where each comes from. Without `POND`, shows every Pond and the Catchment defaults.

## `set`

```bash
duckstring duck set POND [-m N] [options] [--clear]
```

Sets an override for a Pond. Only the options given change. Overrides persist across redeploys.

| Option | Description |
|---|---|
| `--duck` | Where the Pond runs: `catchment` (the Catchment's own machine), a pool name, or `dedicated` (its own machine). A pool that doesn't exist falls back to `catchment`. |
| `--instance-type` | The EC2 instance type for a dedicated Duck running on EC2. |
| `--auto-stop` / `--no-auto-stop` | Whether a dedicated machine stops after each run. |
| `--flock` | `off`, `upgrade` or `always`. See [`[flock]`](../pond_toml.md#flock). |
| `--engine` | The Flock engine, such as `athena`. |
| `--oom` | `fail_up` or `fail`. |
| `--clear` | Remove the Pond's override, reverting to `pond.toml` and the Catchment defaults. |

## `pool`

Pools are named sizes of remote compute, referred to by `duck = "name"` in `pond.toml` or `duck set --duck name`. Four built-in Fargate pools are always available without defining them. Each Pond using a built-in pool gets its own Fargate task of that size:

| Pool | vCPU | Memory |
|---|---|---|
| `S` | 0.5 | 2 GiB |
| `M` | 1 | 4 GiB |
| `L` | 2 | 8 GiB |
| `XL` | 4 | 16 GiB |

### `pool ls`

```bash
duckstring duck pool ls
```

Lists the built-in and defined pools.

### `pool add`

```bash
duckstring duck pool add NAME [--provider fargate|ec2] [options]
```

Creates or updates a pool. The Ponds assigned to a defined pool share one machine of its size, so they can read each other's output locally. A Pond that needs its own machine can use a built-in pool or `--duck dedicated`.

| Option | Description |
|---|---|
| `--provider` | `fargate` (default) or `ec2`. EC2 suits needs beyond Fargate's limits, such as more than 16 vCPU or 120 GiB, or GPUs. |
| `--cpu` | Fargate task CPU units; 1024 is one vCPU. |
| `--memory` | Fargate task memory, in MiB. |
| `--instance-type`, `-t` | EC2 instance type. |
| `--region` | AWS region. Defaults to the Catchment's. |
| `--min`, `--max`, `--keep-warm`, `--idle-timeout` | Scaling settings. They're stored with the pool, but automatic scaling isn't implemented yet. |

### `pool rm`

```bash
duckstring duck pool rm NAME
```

Removes a pool. Ponds that referred to it run on the Catchment's machine instead.
