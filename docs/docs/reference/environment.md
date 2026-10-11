---
title: Environment Variables
description: Every setting read from the environment.
---

# Environment Variables

These variables are read by the Catchment process, and passed on to its Ducks where relevant. Most have an equivalent CLI option on `catchment init`, which takes precedence when given. AWS credentials use the standard `AWS_*` variables, or `AWS_*` entries in the [secret store](cli/secret.md).

## Catchment

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_STATE_ROOT` | `./.duckstring` | The state directory, for a Catchment started through the ASGI entry point (`duckstring.catchment.asgi`). Must be a local path. `DUCKSTRING_ROOT` is an alias. |
| `DUCKSTRING_DATA_ROOT` | under the state directory | Where published tables are stored. See [Data root URIs](formats.md#data-root-uris). |
| `DUCKSTRING_STATE_BACKUP_URI` | none | Where state checkpoints are copied, so a Catchment on disposable storage can recover. A Catchment starting with an empty state directory restores from it. |
| `DUCKSTRING_CHECKPOINT_INTERVAL` | `60s` | How often the state database is copied to the backup. |
| `DUCKSTRING_API_KEY` | none | A single full-access API key. Leave unset when a hosting platform authenticates requests. |
| `DUCKSTRING_CATCHMENT_NAME` | none | A display name recorded for the Catchment. |
| `DUCKSTRING_CATCHMENT_URL` | the bind address | The address local Ducks use to reach the Catchment. Normally unnecessary: when unset, the Catchment learns its address from the first request it serves. |
| `DUCKSTRING_CATCHMENT_PUBLIC_URL` | none | The address remote Ducks use to reach the Catchment. Needed when cloud Ducks can't reach the bind address and the automatic relay isn't used. |
| `DUCKSTRING_FORCE_TAKEOVER` | off | `1` lets a Catchment start on a data root that another live Catchment has claimed. Only for recovering from a Catchment that is known to be gone. |
| `DUCKSTRING_MIN_FREE_BYTES` | `1073741824` (1 GiB) | When free disk space falls below this, the working databases of idle Ponds are removed, least recently used first, and rebuilt when next needed. `0` disables this. |

Run only one Catchment process against a state directory, and only one Catchment against a data root.

## Data plane

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_COMPACT_THRESHOLD` | `268435456` (256 MiB) | The size a merge Trickle's change log must reach before it's folded into the table's base. Can be set per table with `merge_table(compact_threshold=...)`. |
| `DUCKSTRING_S3_ENDPOINT` | none | An S3-compatible endpoint, such as MinIO, for the data root. Equivalent to `?endpoint=` on the URI. |

## Ducks

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_MEMORY_LIMIT` | DuckDB's default | Memory limit for each Duck's DuckDB, such as `12GB`. Also sets the size at which the Flock takes over a computation. Set it to about 80% of the Duck's memory. |
| `DUCKSTRING_DUCK_LAUNCHER` | built in | `module:Class` of a custom launcher that replaces how Ducks are started. |
| `DUCKSTRING_DUCK_ORPHAN_MINUTES` | `60` | How long an idle Duck goes without contact from its Catchment before it exits, so a Duck whose Catchment is gone for good doesn't run indefinitely. A Duck mid-run finishes first. `0` keeps Ducks waiting forever. Set it on the Catchment, which passes it to every Duck, including cloud ones. Pool machines aren't affected. |
| `DUCKSTRING_DISABLE_DUCKS` | off | Start no Ducks at all. For testing the Catchment on its own. |
| `DUCKSTRING_DISCOVER_TIMEOUT` | `300` | Seconds a deploy waits for the Pond's code to load while its Ripples are discovered. |

## Serving

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_SERVE_PG_PORT` | none | Serve the catalog over the Postgres wire protocol on this port. |
| `DUCKSTRING_SERVE_FLIGHT_PORT` | none | Serve the catalog over Arrow Flight on this port, with the SQL as the ticket. Needs `pyarrow` with Flight support. |
| `DUCKSTRING_SERVE_HOST` | `127.0.0.1` | Address both servers bind to. Put TLS and network restrictions in front of them when exposing them. |

## Flock

Set these on the Catchment. It sends them to each Duck with every run, together with any credentials the engine needs, so Ducks in the cloud need no Flock settings of their own. See [Offloading Large Recomputes](../guides/offloading_large_recomputes.md).

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_FLOCK_MODE` | `off` | Catchment default for [`[flock] mode`](pond_toml.md#flock). |
| `DUCKSTRING_FLOCK_ENGINE` | the first configured | Catchment default engine: `motherduck`, `motherduck/<profile>`, `starrocks`, `athena`, or `module:Class`. Unset, the first of `motherduck`, `starrocks` and `athena` that is configured. An engine that can't be loaded turns the Flock off. |
| `DUCKSTRING_FLOCK_OOM_POLICY` | `fail_up` | Catchment default for `[flock] oom_policy`. |
| `DUCKSTRING_FLOCK_ACCEPT` | | Catchment default for `[flock] accept`, comma-separated. |
| `DUCKSTRING_FLOCK_MIN_ROWS` | from `DUCKSTRING_MEMORY_LIMIT` | The row count above which `upgrade` mode sends a computation to the Flock up front. |
| `DUCKSTRING_FLOCK_VERIFY_KEYS` | `1000` | How many keys of a StarRocks or Athena result are recomputed in the Duck and compared before it's used. `0` turns verification off. |
| `DUCKSTRING_FLOCK_MOTHERDUCK_DATABASE` | `duckstring_flock` | The MotherDuck database that sources are uploaded to for a job. |
| `DUCKSTRING_FLOCK_STARROCKS_HOST` | | StarRocks frontend host. |
| `DUCKSTRING_FLOCK_STARROCKS_PORT` | `9030` | StarRocks MySQL-protocol port. |
| `DUCKSTRING_FLOCK_STARROCKS_USER` | | StarRocks user. The password is the secret `STARROCKS_PASSWORD`. |
| `DUCKSTRING_FLOCK_STARROCKS_WAREHOUSE` | | Warehouse to run in. |
| `DUCKSTRING_FLOCK_STARROCKS_SCRATCH` | | S3 prefix for staged sources and results. |
| `DUCKSTRING_FLOCK_STARROCKS_REGION` | | Region of the scratch bucket. |
| `DUCKSTRING_FLOCK_STARROCKS_S3_ENDPOINT` | | The scratch bucket's endpoint as StarRocks sees it, for an S3-compatible store. |
| `DUCKSTRING_FLOCK_STARROCKS_TIMEOUT` | `900` | Seconds a job may run on StarRocks, including a suspended warehouse resuming. |
| `DUCKSTRING_FLOCK_ATHENA_WORKGROUP` | | Athena workgroup. |
| `DUCKSTRING_FLOCK_ATHENA_DATABASE` | | Athena database. |
| `DUCKSTRING_FLOCK_ATHENA_SCRATCH` | | S3 location for staged sources and Athena results. |
| `DUCKSTRING_FLOCK_ATHENA_REGION` | | Athena region. |

The engines' credentials are secrets (`duckstring secret set`), sent with each run only while the Pond's Flock is on: `MOTHERDUCK_TOKEN` (or `MOTHERDUCK_TOKEN_<PROFILE>` for `motherduck/<profile>`), and `STARROCKS_PASSWORD`, `STARROCKS_S3_ACCESS_KEY` and `STARROCKS_S3_SECRET_KEY`. Athena uses the Duck's IAM role.

## Cloud Ducks on Fargate

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_FARGATE_IMAGE` | | Container image for Ducks. Required unless `DUCKSTRING_FARGATE_TASK_DEF` is set. |
| `DUCKSTRING_FARGATE_TASK_DEF` | | An existing task definition to run instead of registering one. |
| `DUCKSTRING_FARGATE_CLUSTER` | `default` | ECS cluster. |
| `DUCKSTRING_FARGATE_SUBNETS` | | Subnets for Duck tasks. Required. |
| `DUCKSTRING_FARGATE_SECURITY_GROUPS` | | Security groups for Duck tasks. Ducks only make outbound connections, so no inbound rules are needed. |
| `DUCKSTRING_FARGATE_EXECUTION_ROLE` | | Task execution role. Required. |
| `DUCKSTRING_FARGATE_TASK_ROLE` | | Role the Duck runs as, which needs access to the data root. Required. |
| `DUCKSTRING_FARGATE_ASSIGN_PUBLIC_IP` | `ENABLED` | Whether tasks get a public IP. |
| `DUCKSTRING_FARGATE_CPU_ARCH` | `X86_64` | `X86_64` or `ARM64`. |
| `DUCKSTRING_FARGATE_CPU` | `1024` | CPU units for tasks not using a pool's size. |
| `DUCKSTRING_FARGATE_MEMORY` | `4096` | Memory in MiB for tasks not using a pool's size. |

## Cloud Ducks on EC2

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_EC2_AMI` | | Machine image for Ducks. Its default `python3` must be 3.10 or newer, with a matching `pip3`. Required. |
| `DUCKSTRING_EC2_INSTANCE_PROFILE` | | Instance profile the Duck runs as. Required. |
| `DUCKSTRING_EC2_INSTANCE_TYPE` | `m6i.large` | Instance type when a pool doesn't set one. |
| `DUCKSTRING_EC2_PIP_SPEC` | | What to `pip install` on boot, when the image doesn't already include Duckstring. |
| `DUCKSTRING_EC2_SUBNET` | | Subnet for Duck instances. |
| `DUCKSTRING_EC2_SECURITY_GROUPS` | | Security groups for Duck instances. Without them, instances use the VPC's default group and usually can't reach the Catchment. |
| `DUCKSTRING_EC2_ASSIGN_PUBLIC_IP` | | Whether instances get a public IP. |

## Relay

When a Catchment on a private network (such as a laptop) runs cloud Ducks, it can start a small EC2 relay that the Ducks connect to, with an SSH tunnel back to the Catchment.

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_RELAY` | on | `off` disables the relay. |
| `DUCKSTRING_RELAY_AMI` | `DUCKSTRING_EC2_AMI` | Machine image for the relay. |
| `DUCKSTRING_RELAY_INSTANCE_TYPE` | `t4g.nano` | Relay instance type. |
| `DUCKSTRING_RELAY_PORT` | the Catchment's port | Port the relay listens on. |
| `DUCKSTRING_RELAY_TTL_MINUTES` | `30` | The relay shuts itself down after this long without the tunnel. |
| `DUCKSTRING_RELAY_KEY_NAME` | | EC2 key pair for the relay. |
| `DUCKSTRING_RELAY_SSH_KEY` | | Local private key file for the tunnel. |
| `DUCKSTRING_RELAY_SSH_USER` | `ec2-user` | SSH user on the relay. |
| `DUCKSTRING_RELAY_SECURITY_GROUP` | | Security group for the relay. |

## Email alerts

Defaults for `mailto:` alert channels. See [Notification URIs](formats.md#notification-uris).

| Variable | Default | Description |
|---|---|---|
| `DUCKSTRING_SMTP_HOST` | | SMTP server as `host:port`. |
| `DUCKSTRING_SMTP_FROM` | `duckstring@localhost` | Sender address. |
| `DUCKSTRING_SMTP_USER` | | SMTP username. |
| `DUCKSTRING_SMTP_PASSWORD` | | SMTP password. |
| `DUCKSTRING_SMTP_TLS` | `1` | `0`, `false` or `no` turns off STARTTLS. |
