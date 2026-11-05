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
| `source.table` | Python API, `query`, `get`, `data`, `pond puddle show` | A table (or Object) published by the Pond `source`. |
| ``source.`name.with.dots` `` | Python API, `@puddle` targets | Backticks take a part literally, for a table or Object name containing dots. Either part can be quoted. |
| `name@major` | `control`, `/api/status` ids | One major version line of a Pond. |
| `{pond}_v{major}.table` | catalog SQL | A table in a specific major line. |
| `{pond}.table` | catalog SQL | A table in the Pond's served major. |
| `{pond}#{spout}` | `window` | A Spout, which is managed as its own node. |

An unquoted reference is split at its first dot, so `sales.daily.v2` is the table `daily.v2` of `sales`, while `daily.v2` alone is a table of the Source `daily`. Names and columns beginning with `_duckstring_` are reserved for Duckstring.

## Versions

Pond versions are semantic versions, `MAJOR.MINOR.PATCH`. In `[sources]`, a version pins a Source's major line and minimum version, and a trailing `?` makes the Source optional. See [`pond.toml`](pond_toml.md#sources).

## Durations

Used by `refresh add --every`, window `--duration`, `alert --stale` and `--renotify`, and `catchment init --checkpoint-every`.

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
| `s3://bucket/prefix` | Amazon S3 or an S3-compatible store. Needs the `duckstring[aws]` extra. |
| `gs://bucket/prefix` | Google Cloud Storage. Needs the `duckstring[gcp]` extra. |
| `az://container/prefix` | Azure Blob Storage, in the account given by `account_name`. Needs the `duckstring[azure]` extra. |
| `abfss://container@account.dfs.core.windows.net/prefix` | Azure Data Lake Storage. Needs the `duckstring[azure]` extra. |

Credentials and options are query parameters on the URI. Ducks read the data root as well as the Catchment, so a credential in a data root can only be an `${env:NAME}` reference, and the variable must be set wherever Ducks run. Without credentials in the URI, each cloud's usual credentials are used, such as an instance role, an attached service account or a managed identity.

### S3

| Parameter | Description |
|---|---|
| `key_id` | Access key ID. Without `key_id` and `secret`, the standard AWS credential chain is used. |
| `secret` | Secret access key. |
| `region` | Region. Defaults to `AWS_REGION` or `AWS_DEFAULT_REGION`. |
| `endpoint` | An S3-compatible endpoint such as `http://minio:9000`, using path-style addressing. Also settable as `DUCKSTRING_S3_ENDPOINT`. |

```text
s3://my-lake/duckstring?region=eu-west-2&key_id=${env:AWS_KEY}&secret=${env:AWS_SECRET}
```

### Google Cloud Storage

| Parameter | Description |
|---|---|
| `credentials` | A service-account key: the path to its JSON file, or the JSON itself as a reference. Without it, Google's application default credentials are used: `GOOGLE_APPLICATION_CREDENTIALS`, the service account attached to the machine, or a `gcloud auth application-default login`. `anon` reads a public bucket without credentials. |
| `key_id`, `secret` | HMAC keys. They cover reading and writing tables, but listing and small files still need Google credentials, so a data root can't use HMAC keys alone. |
| `endpoint` | Another endpoint, such as an emulator. |

DuckDB reads and writes with an access token made from the Google credentials, which is renewed while less than 40 minutes of its hour remain.

```text
gs://my-lake/duckstring
gs://my-lake/duckstring?credentials=/etc/duckstring/gcs-key.json
```

### Azure

| Parameter | Description |
|---|---|
| `account_name` | The storage account, for an `az://` URI. Defaults to `AZURE_STORAGE_ACCOUNT_NAME`. An `abfss://` URI names its account. |
| `connection_string` | A connection string. Defaults to `AZURE_STORAGE_CONNECTION_STRING`. |
| `account_key` | An account key. Defaults to `AZURE_STORAGE_ACCOUNT_KEY`. |
| `sas_token` | A shared access signature. Defaults to `AZURE_STORAGE_SAS_TOKEN`. |
| `tenant_id`, `client_id`, `client_secret` | A service principal. Default to `AZURE_STORAGE_TENANT_ID`, `AZURE_STORAGE_CLIENT_ID` and `AZURE_STORAGE_CLIENT_SECRET`. |

The first credential that is set is used, in the order of the table. With none, the Azure default credential chain is used: the `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` variables, workload identity, a managed identity, or the Azure CLI's login.

```text
abfss://lake@acmedata.dfs.core.windows.net/duckstring
az://lake/duckstring?account_name=acmedata&account_key=${env:AZURE_KEY}
```

## Destination URIs

Where a [Spout](cli/spout.md) delivers. The scheme picks the writer.

### `file://`

```text
file:///srv/exports/sales
```

Writes `{table}.parquet` into the directory, replacing each file atomically.

### Object storage

```text
s3://bucket/prefix?region=eu-west-2
gs://bucket/prefix
abfss://exports@acmedata.dfs.core.windows.net/duckstring?client_id=${secret:SP_ID}&tenant_id=${secret:SP_TENANT}&client_secret=${secret:SP_SECRET}
```

`s3://`, `gs://`, `az://` and `abfss://` write `{prefix}/{table}.parquet`, or with `--mode append`, the table's per-run files. They take the parameters of a [data root](#data-root-uris) in the same store, and credentials can also be `${secret:NAME}` references, since Spouts deliver from the Catchment. A `gs://` Spout that writes whole tables can use HMAC keys alone; one with `--mode append` also needs Google credentials. `s3://` also takes:

| Parameter | Description |
|---|---|
| `session_token` | An S3 session token. |
| `url_style`, `use_ssl` | Override the addressing style (`path` or `vhost`) and TLS for an `endpoint`. |

### `postgres://`

```text
postgres://loader:${secret:PG_PASSWORD}@db.internal:5432/analytics?schema=duckstring
```

A standard libpq connection URI (`postgresql://` also works). Tables are created in the `schema` query parameter (default `public`), and delivery progress is recorded in a `_duckstring_egress` table there. The table being delivered needs a primary key.

### `ducklake:`

```text
ducklake:sqlite:/srv/lake/meta.sqlite?data_path=s3://lakehouse/files/&schema=sales
ducklake:postgres:dbname=lake host=pg.internal user=loader password=${secret:LAKE_PG}?data_path=s3://lakehouse/files/
ducklake:/srv/lake/meta.ducklake
```

The string DuckDB's `ATTACH` takes for a [DuckLake](https://ducklake.select), followed by Duckstring's parameters. Any other query parameter stays on the attach string, such as `sslmode` for a Postgres catalog written as a URI.

| Parameter | Description |
|---|---|
| `data_path` | Where a new lake keeps its data files. An existing lake uses the path it was created with. |
| `schema` | The schema tables are delivered to (default `main`). Delivery progress is recorded in a `_duckstring_egress` table there. |
| store credentials | Credentials for an object-store `data_path`, with the same parameters as an [object-storage destination](#object-storage). |

Any table can be delivered: a merge Trickle by its primary key, an append Trickle's new rows, and a plain table replaced whole. Each delivery is one transaction, so it adds one snapshot to the lake, with author `duckstring` and the Pond as `{name}@{major}` in the commit message. The catalog is never migrated. A lake created under DuckDB 2.0 uses DuckLake 1.1, which DuckDB 1.5 can't read. It needs DuckDB's `ducklake` extension, downloaded on first use.

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
