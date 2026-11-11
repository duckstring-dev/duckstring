---
title: Offloading Large Recomputes
description: Send a recompute that won't fit in a Duck to MotherDuck, StarRocks, Databricks, BigQuery or Athena with the Flock.
---

# Offloading Large Recomputes

A Trickle usually updates from a small change, but some runs recompute the whole output: the first run, a rebuild, a source whose changes have aged out of reach, or a change touching most of a source. A `.sql()` step is recomputed in full on every run. When that recompute is larger than the Duck's memory, the Pond fails or its Duck has to be sized for its worst run rather than its usual one.

The **Flock** sends such a recompute to a larger engine. The Duck keeps everything else: it works out what changed, writes the change log and publishes. So a Pond can run on a small Duck and still handle the occasional run that needs a much bigger machine.

Only a run that recomputes wholesale anyway is sent. An incremental run is small by design and always runs in the Duck.

## What gets sent

The Flock takes a builder terminal's full recompute, as one query:

| Builder | What the engine computes | What comes back |
|---|---|---|
| Joins, `.filter()`, `.mutate()`, `.select()` | the whole output | the output's rows |
| `.aggregate()` | the per-group running totals the aggregate keeps (count, sums, centred moments, extremes) | one row per group |
| `.sql()` | the query over the result before it | the query's rows |

Sending an aggregate's per-group totals, rather than its input, keeps the largest relation in the job on the engine. `.accumulate()` and `agg.reduce` are Python folds and always run in the Duck, as does SQL you run directly on `pond.con`.

## Turning it on

Each Pond chooses how it uses the Flock, in `pond.toml`:

```toml
[flock]
mode = "upgrade"
```

| Mode | Behaviour |
|---|---|
| `off` | Everything runs in the Duck (the default). |
| `upgrade` | A recompute that is clearly too big for the Duck is sent up front. Anything else runs in the Duck, and if it runs out of memory it is sent instead of failing. |
| `always` | Every eligible recompute is sent. |

"Clearly too big" is measured in source rows and derived from the Duck's memory limit, so set `DUCKSTRING_MEMORY_LIMIT` on Ducks that use the Flock (about 80% of their memory), or set the row count directly with `DUCKSTRING_FLOCK_MIN_ROWS`. `[flock] oom_policy = "fail"` turns off the retry after running out of memory, so the memory limit becomes a hard cap.

An operator can override any of these per Pond without a redeploy:

```bash
duckstring duck set priced --flock upgrade
```

The Flock needs an engine configured on the Catchment (below). Without one, a Pond's `[flock]` settings have no effect, so the same `pond.toml` works on a laptop.

## Choosing an engine

Configure one or more engines on the Catchment. A Pond uses the first configured one in this order, unless it names one with `[flock] engine` or `duckstring duck set --engine`:

| Engine | What it runs | What it can take | Data |
|---|---|---|---|
| `motherduck` | DuckDB, in MotherDuck | everything the builder expresses in SQL | read in place from S3 or GCS, or uploaded by the Duck |
| `starrocks` | StarRocks (CelerData or self-hosted) | every builder shape, with expressions measured to agree | staged under a scratch prefix in your bucket |
| `databricks` | Databricks SQL, on a SQL warehouse on any cloud | every builder shape, with expressions measured to agree | staged in a Unity Catalog Volume |
| `bigquery` | Google BigQuery | every builder shape, with expressions measured to agree | staged under a scratch prefix in Cloud Storage |
| `athena` | Trino, in Amazon Athena | every builder shape, with a short list of measured expressions | staged under a scratch prefix in your bucket |

MotherDuck runs DuckDB, so a job's SQL runs unchanged and its results match by construction. The other engines aren't DuckDB, so each job is translated to their SQL, and each construct in it (a function, an operator, a cast) is sent only if its translation was measured to give DuckDB's answer. A job containing anything else runs in the Duck, with the construct named in the log. The [section on accuracy](#accuracy-on-other-engines) has the detail.

The Catchment sends each Pond's Flock settings, and its engine's credentials, to the Duck with every run. Cloud Ducks need no Flock configuration of their own, and a rotated credential takes effect from the next run.

## MotherDuck

Store a MotherDuck token as a secret on the Catchment:

```bash
duckstring secret set MOTHERDUCK_TOKEN
```

That is enough to make `motherduck` the default engine. MotherDuck's own `motherduck_token` environment variable also works.

A Duckling's size is set per MotherDuck account, and resizing takes minutes, so pick a size by picking an account. Give each size its own service account and token, stored as `MOTHERDUCK_TOKEN_<PROFILE>`, and point a heavy Pond at it:

```bash
duckstring secret set MOTHERDUCK_TOKEN_GIGA
duckstring duck set priced --engine motherduck/giga
```

A Duckling stays running, and billed, for its cooldown after the last query. Dispatches are rare, so set the cooldown on these accounts to the minimum of one minute.

Sources published on an S3 or GCS data root are read by MotherDuck where they are, once MotherDuck can reach the bucket. Create a secret for it in MotherDuck once, scoped as narrowly as your setup allows:

```sql
CREATE SECRET IN MOTHERDUCK (TYPE S3, KEY_ID '...', SECRET '...', REGION 'ap-southeast-2', SCOPE 's3://acme-lake');
```

Anything MotherDuck can't read in place is uploaded by the Duck for the job and dropped afterwards: the Pond's own tables, a source published only on the Duck's machine, a Source whose copy on the data root hasn't caught up yet, and tables a `.sql()` query reads by name. Uploads go to a MotherDuck database, `duckstring_flock` by default (`DUCKSTRING_FLOCK_MOTHERDUCK_DATABASE`). With a local data root, every source is uploaded.

Data sent to MotherDuck is processed in MotherDuck's cloud, outside your own cloud account. If it must stay in your account, use one of the other engines.

## StarRocks

StarRocks is a distributed engine for recomputes beyond what one DuckDB machine can hold. AWS has no managed StarRocks; two setups work well:

- **CelerData Cloud BYOC**, from the StarRocks authors, deploys into your own VPC and bills per compute unit while a warehouse runs. Create a dedicated warehouse for the Flock with auto-suspend on. The default warehouse can't be suspended on its own, and a warehouse billed while idle costs far more than the occasional recompute is worth.
- **Self-hosted StarRocks in shared-data mode**, on EC2 or EKS, with S3 as its storage.

Install the client with `pip install 'duckstring[starrocks]'` (Pond environments get it from the Catchment's own install) and configure the Catchment:

```bash
DUCKSTRING_FLOCK_STARROCKS_HOST=starrocks.internal
DUCKSTRING_FLOCK_STARROCKS_USER=duckstring
DUCKSTRING_FLOCK_STARROCKS_WAREHOUSE=flock
DUCKSTRING_FLOCK_STARROCKS_SCRATCH=s3://acme-lake/flock-scratch
DUCKSTRING_FLOCK_STARROCKS_REGION=ap-southeast-2
```

```bash
duckstring secret set STARROCKS_PASSWORD
```

For each job the Duck writes its sources as Parquet under the scratch prefix, StarRocks reads them with `FILES()` and writes the result back as Parquet, and the Duck reads that. StarRocks reaches the bucket with its own instance profile. To use keys instead, store them as the secrets `STARROCKS_S3_ACCESS_KEY` and `STARROCKS_S3_SECRET_KEY`. Give the scratch prefix a lifecycle rule as a backstop, since every job's files are temporary and removed when it finishes.

A query sent to a suspended warehouse waits for it to resume, which counts against the job's timeout (`DUCKSTRING_FLOCK_STARROCKS_TIMEOUT`, 900 seconds by default).

## Databricks

The Flock can run on a Databricks SQL warehouse, on any cloud. Install the client with `pip install 'duckstring[databricks]'` and configure the Catchment:

```bash
DUCKSTRING_FLOCK_DATABRICKS_HOST=https://adb-1234567890123456.7.azuredatabricks.net
DUCKSTRING_FLOCK_DATABRICKS_WAREHOUSE=1a2b3c4d5e6f7a8b
DUCKSTRING_FLOCK_DATABRICKS_VOLUME=/Volumes/main/duckstring/flock
```

The host defaults to `DATABRICKS_HOST`, which a Databricks App sets. For each job the Duck writes its sources as Parquet and uploads them to the Volume through the Files API, the warehouse reads them with `read_files`, and the result comes back through the Statement Execution API as Arrow. Each job's files are removed from the Volume when it finishes. No external location or storage credential is involved, but the result is downloaded from links into the workspace's own storage, so a Duck behind a restrictive firewall needs to reach that storage as well as the workspace.

Store the credentials as secrets, either a service principal's OAuth credentials or a personal access token:

```bash
duckstring secret set DATABRICKS_CLIENT_ID
duckstring secret set DATABRICKS_CLIENT_SECRET
```

On Azure Databricks, Ducks without these sign in with their own Microsoft Entra identity instead; [Cloud Compute on Azure](cloud_compute_on_azure.md#the-flock-on-azure-databricks) sets that up. Whichever identity is used needs **Can use** on the warehouse, and `READ VOLUME` and `WRITE VOLUME` on the Volume, with `USE CATALOG` and `USE SCHEMA` on its catalog and schema.

Use a serverless SQL warehouse, which starts in seconds and stops itself when idle. On a 2X-Small serverless warehouse a job took about 1.2 seconds once the warehouse was running, and 12.7 seconds for the first. A job's timeout, `DUCKSTRING_FLOCK_DATABRICKS_TIMEOUT` (900 seconds by default), includes the warehouse starting.

Databricks takes every builder shape and most expressions. A few constructs whose results differ on Databricks run in the Duck, among them `floor` and `ceil` of a floating-point value, `date_trunc`, `median` and `strftime`.

## BigQuery

Install the client with `pip install 'duckstring[gcp]'` and configure the Catchment:

```bash
DUCKSTRING_FLOCK_BIGQUERY_DATASET=duckstring_flock
DUCKSTRING_FLOCK_BIGQUERY_SCRATCH=gs://acme-lake/flock-scratch
DUCKSTRING_FLOCK_BIGQUERY_LOCATION=australia-southeast1
```

Jobs run in the credentials' project unless `DUCKSTRING_FLOCK_BIGQUERY_PROJECT` names another. For each job the Duck writes its sources as Parquet under the scratch prefix, BigQuery reads them through temporary external tables in the dataset and writes the result back under the prefix with `EXPORT DATA`, and the Duck reads it. The tables and files are removed when the job finishes. Keep the dataset in the scratch bucket's location, and give it a default table expiration as a backstop.

The Duck uses its own Google credentials, or a service-account key named by `credentials=` on the scratch URI, as for a [`gs://` data root](../reference/formats.md#data-root-uris). It needs BigQuery Job User on the project, BigQuery Data Editor on the dataset, and read and write access to the scratch prefix. [Cloud Compute on Google Cloud](cloud_compute_on_google_cloud.md#the-flock-on-bigquery) sets this up.

When measured, each job took 3 to 5 seconds, and BigQuery billed each at its 10 MB minimum. BigQuery takes every builder shape and most expressions. A few constructs whose results differ on BigQuery run in the Duck, among them a cast of a decimal to text, `%` on floating-point values, `first_value` and `median`.

## Athena

```bash
DUCKSTRING_FLOCK_ATHENA_WORKGROUP=duckstring-flock
DUCKSTRING_FLOCK_ATHENA_DATABASE=duckstring_flock
DUCKSTRING_FLOCK_ATHENA_SCRATCH=s3://acme-lake/flock-scratch
DUCKSTRING_FLOCK_ATHENA_REGION=ap-southeast-2
```

Sources are staged under the scratch prefix and registered as temporary Glue tables, and the result comes back through an `UNLOAD`. The Duck's IAM role needs Athena and Glue access, plus `s3:GetBucketLocation` and `s3:ListBucketMultipartUploads` on the scratch bucket; without those, Athena reports "Unable to verify/create output bucket". [Cloud Compute on AWS](cloud_compute_on_aws.md#the-flock-on-athena) covers the roles.

Athena takes every builder shape, but its expression list is short: arithmetic other than division, `round`, `abs`, `floor`, `ceil`, `coalesce`, `nullif`, `least` and `greatest`. Aggregates haven't been measured on Athena, so an `.aggregate()` stays in the Duck.

## Accuracy on other engines

DuckDB is the authority on what a Pond publishes; the Flock only decides where the work runs. Four checks keep the answer of an engine other than DuckDB equal to DuckDB's.

1. **Measured constructs.** Each engine has a list of the constructs it was measured to agree on, run through the same translation the engine uses. Where an engine's own semantics differ, the translation spells out DuckDB's: on StarRocks, `/` becomes floating-point division with DuckDB's results for a zero divisor, `CAST` to an integer rounds as DuckDB does, and `var_samp` stays the sample variance. A value the engine can't represent (DuckDB's NaN from `0/0`) stops the job, which then runs in the Duck.
2. **Named differences.** Some differences are known and bounded, and a Pond can accept them by name (below).
3. **The schema.** The result is cast to the column types DuckDB would have produced, and rejected if its columns differ.
4. **Sampled verification.** Before the result is used, the Duck recomputes a random sample of its keys and compares them both ways, so missing, extra and wrong rows are caught.

A result that fails any check is discarded and the job runs in the Duck, so a problem shows up as a slower run rather than in your data.

Verification samples 1,000 keys by default (`DUCKSTRING_FLOCK_VERIFY_KEYS`; `0` turns it off). It works when the output's key passes through unchanged from one source column, as with `pk="order_id"` from `orders.order_line`; the Duck then reads only the sampled keys' rows from that source. When the key is computed, or comes from a `.sql()` query, verification is skipped and logged. A sample catches a difference that affects every row, such as a different division rule, and can miss one that affects a rare value; it adds to the measured list rather than replacing it.

### Accepting known differences

| Name | Difference |
|---|---|
| `float_order` | Floating-point sums, averages and the statistics built on them (variance, standard deviation, covariance, correlation), and `ln`, `exp`, `pow` and `log`, can differ in the last bits. A distributed engine adds in a different order, and DuckDB isn't bit-stable across thread counts either. |
| `half_rounding` | A `CAST` to an integer of a value exactly halfway between two integers rounds differently. |

A Pond that accepts a difference lets jobs containing it be sent:

```toml
[flock]
mode = "upgrade"
accept = ["float_order"]
```

or `duckstring duck set priced --accept float_order`. Whether last-bit differences matter depends on the data, so this is the Pond author's call, and nothing is accepted by default. With `float_order` accepted, verification compares floating-point columns within a relative 1e-9.

An `.aggregate()` with a `sum` or `mean` keeps the centred second moment it would need for a variance, which is computed in floating point, so on StarRocks, Databricks and BigQuery it needs `float_order`. One with only counts, minimums and maximums doesn't.

A difference that changes what kind of value comes back (an integer where DuckDB gives a fraction, NULL where DuckDB raises an error, or the reverse) is never on an engine's list, and no name can accept it.

## Watching it

A failed or rejected dispatch runs in the Duck, so it never fails the Pond, but it does cost the time the Flock was meant to save. Watch `duckstring_flock_dispatch_failures_total` on [`/metrics`](monitoring_and_failures.md#metrics), and the `flock_error` field in a Pond's status, which gives the last reason: the engine's error, a schema mismatch, or how many sampled keys disagreed. Constructs an engine won't take are logged by the Duck at info level.
