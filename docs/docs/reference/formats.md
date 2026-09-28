---
title: Formats
description: Durations, references and URIs accepted across Duckstring.
---

# Formats

Value formats shared by `pond.toml`, the CLI, the Python API and the HTTP API.

## Table and Pond references

| Form | Used in | Meaning |
|---|---|---|
| `table` | Python API | One of the current Pond's own tables. |
| `source.table` | Python API, `trace`, `puddle show` | A table published by the Pond `source`. |
| ``source.`name.with.dots` `` | Python API, `@puddle` targets | Backticks take a part literally, for a table or Object name containing dots. Either part can be quoted. |
| `name@major` | `do`, `/api/status` ids | One major version line of a Pond. |
| `{pond}_v{major}.table` | catalog SQL | A table in a specific major line. |
| `{pond}.table` | catalog SQL | A table in the Pond's served major. |
| `{pond}#{spout}` | `trigger window` | A Spout, which is managed as its own node. |

An unquoted reference is split at its first dot, so `sales.daily.v2` is the table `daily.v2` of `sales`, while `daily.v2` alone is a table of the Source `daily`. Names and columns beginning with `_duckstring_` are reserved for Duckstring.

## Versions

Pond versions are semantic versions, `MAJOR.MINOR.PATCH`. In `[sources]`, a version pins a Source's major line and minimum version, and a trailing `?` makes the Source optional. See [`pond.toml`](pond_toml.md#sources).

## Durations

Used by `trigger tide`, window `--duration`, `alert --stale` and `--renotify`, and `catchment init --checkpoint-every`.

A duration is one or more number-and-unit pairs with no spaces: `30s`, `45m`, `12h`, `1d`, `2w`, `1h30m`.

| Unit | Meaning |
|---|---|
| `s` | seconds |
| `m` | minutes |
| `h` | hours |
| `d` | days |
| `w` | weeks |

A window's `--every` takes a single pair only, such as `1d` or `12h`.

## Times

Window `--start` and `--until` take ISO 8601 timestamps, such as `2026-10-01T02:00:00+00:00`. `--start` also accepts `HH:MM`, meaning that time today in UTC. Freshness values in the API and run history are ISO 8601 in UTC.

## Credential references

Destination URIs never contain credentials directly. They contain references, resolved on the Catchment only when the credential is used:

| Reference | Resolves to |
|---|---|
| `${env:NAME}` | The environment variable `NAME` on the Catchment process. |
| `${secret:NAME}` | The secret `NAME` from the Catchment's [secret store](cli/secret.md). |

A reference can also be the whole destination, such as `${env:DATABASE_URL}` or `${secret:SLACK_WEBHOOK}`, when the entire URI is sensitive or is provided that way. Its scheme is then checked when it's resolved, at the first delivery or test, rather than when the Spout or channel is added.

A missing variable or secret fails the delivery with an error naming the reference, never its value. Any other `${...}` text is left as it is.

## Data root URIs

Where a Catchment stores published tables (`catchment init --data-root`, `catchment settings --data-root`, `DUCKSTRING_DATA_ROOT`).

| Form | Storage |
|---|---|
| a local path | A directory on the Catchment's machine. The default is under the state directory. |
| `/Volumes/...` | A Databricks Volume, used as a local path. |
| `s3://bucket/prefix` | Amazon S3 or an S3-compatible store. |
| `gs://bucket/prefix` | Google Cloud Storage. |
| `abfss://container@account.dfs.core.windows.net/prefix` | Azure Data Lake Storage, configured through the environment. |

Query parameters for `s3://` and `gs://`:

| Parameter | Description |
|---|---|
| `key_id` | Access key ID. Without `key_id` and `secret`, the standard AWS credential chain is used. |
| `secret` | Secret access key. |
| `region` | Region. Defaults to `AWS_REGION` or `AWS_DEFAULT_REGION`. |
| `endpoint` | An S3-compatible endpoint such as `http://minio:9000`, using path-style addressing. Also settable as `DUCKSTRING_S3_ENDPOINT`. |

```text
s3://my-lake/duckstring?region=eu-west-2&key_id=${env:AWS_KEY}&secret=${secret:AWS_SECRET}
```

## Destination URIs

Where a [Spout](cli/spout.md) delivers. The scheme picks the writer.

### `file://`

```text
file:///srv/exports/sales
```

Writes `{table}.parquet` into the directory, replacing each file atomically.

### `s3://` and `gs://`

```text
s3://bucket/prefix?region=eu-west-2
gs://bucket/prefix?key_id=${env:GCS_HMAC_ID}&secret=${env:GCS_HMAC_SECRET}
```

Writes `{prefix}/{table}.parquet`, or with `--mode append`, the table's per-run files.

| Parameter | Description |
|---|---|
| `key_id`, `secret` | Credentials. Required for `gs://` (as HMAC keys). For `s3://`, the AWS credential chain is used without them. |
| `session_token` | An S3 session token. |
| `region` | S3 region. |
| `endpoint`, `url_style`, `use_ssl` | For S3-compatible stores. |

### `postgres://`

```text
postgres://loader:${secret:PG_PASSWORD}@db.internal:5432/analytics?schema=duckstring
```

A standard libpq connection URI (`postgresql://` also works). Tables are created in the `schema` query parameter (default `public`), and delivery progress is recorded in a `_duckstring_egress` table there. The table being delivered needs a primary key.

## Notification URIs

Where an [alert channel](cli/alert.md) sends.

### `https://` and `http://`

Posts a JSON message to the URL. The message has a top-level `text` summary alongside structured fields, so a Slack incoming webhook URL works directly.

### `mailto:`

```text
mailto:oncall@example.com,data@example.com?smtp=smtp.example.com:587&user=alerts&password=${secret:SMTP_PASSWORD}
```

Sends an email to each comma-separated address.

| Parameter | Default | Description |
|---|---|---|
| `smtp` | `DUCKSTRING_SMTP_HOST` | SMTP server as `host:port`. The port defaults to 587. Required one way or the other. |
| `from` | `DUCKSTRING_SMTP_FROM`, else `duckstring@localhost` | Sender address. |
| `user` | `DUCKSTRING_SMTP_USER` | SMTP username. |
| `password` | `DUCKSTRING_SMTP_PASSWORD` | SMTP password. |
| `tls` | `DUCKSTRING_SMTP_TLS`, else on | STARTTLS. `0`, `false` or `no` turns it off. |
