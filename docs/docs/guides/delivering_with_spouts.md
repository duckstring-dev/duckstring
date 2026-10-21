---
title: Delivering with Spouts
description: Deliver a Pond's tables to object storage, Postgres or a DuckLake.
---

# Delivering with Spouts

Some consumers can't query a Catchment: an application reading its own Postgres database, a partner collecting files from a bucket. A Spout delivers a Pond's tables to a destination like these whenever the Pond publishes new output. This guide sets up the common destinations. See [`duckstring spout`](../reference/cli/spout.md) for every option.

## Delivering files

Write a Pond's tables as Parquet files into a directory:

```bash
duckstring spout add reports --to file:///srv/exports/reports
```

Each table is written to `{directory}/{table}.parquet` and replaced whenever `reports` publishes. Deliver one table rather than all of them with `--table monthly_summary`.

<figure class="ds-figure">
  <img src="/img/docs/spout-canvas.webp" alt="The reports Pond on the canvas with a dashed Spout node, exports, after it" width="421" height="182" />
  <figcaption>A Spout is drawn as a dashed node after its Pond.</figcaption>
</figure>

### To object storage

The same works for S3 and Google Cloud Storage:

```bash
duckstring spout add reports --table monthly_summary --to 's3://partner-drop/duckstring?region=eu-west-2'
```

For S3, the Catchment's AWS credentials are used by default. To use other keys, keep them in the [secret store](../reference/cli/secret.md) and reference them in the URI:

```bash
duckstring secret set PARTNER_KEY_ID
duckstring secret set PARTNER_SECRET
duckstring spout add reports --to 's3://partner-drop/duckstring?region=eu-west-2&key_id=${secret:PARTNER_KEY_ID}&secret=${secret:PARTNER_SECRET}'
```

Quote the URI in single quotes so the shell leaves `${...}` alone. References are resolved only when delivering; the Spout stores the reference, and neither `spout ls` nor the UI ever shows a value. `${env:NAME}` reads an environment variable on the Catchment instead. Google Cloud Storage needs HMAC keys as `key_id` and `secret`. S3-compatible stores take `endpoint=`; see [Destination URIs](../reference/formats.md#destination-uris).

By default each delivery rewrites the whole table. For a large append Trickle, `--mode append` copies only each run's new files, keeping Duckstring's file layout in the destination, which suits a consumer reading with DuckDB or another Parquet reader.

## Delivering to Postgres

A Postgres Spout keeps a table in an application's database in sync, sending only the rows that changed:

```bash
duckstring secret set APP_DB_PASSWORD
duckstring spout add priced --table priced_line \
  --to 'postgres://loader:${secret:APP_DB_PASSWORD}@db.internal:5432/app?schema=analytics'
```

If the connection string is already in an environment variable on the Catchment, as many platforms provide it, use the variable as the whole destination:

```bash
duckstring spout add priced --table priced_line --to '${env:DATABASE_URL}'
```

A destination held entirely in a variable or secret is checked when it's first used, by the first delivery or the UI's **Test** button, rather than when the Spout is added.

The table must be a merge Trickle with a primary key, written with `merge_table` or the builder's `.merge()`. Duckstring creates `analytics.priced_line` on first delivery, from the table's columns. Each later delivery deletes the changed and removed keys and inserts their new rows, in a single transaction.

Delivery is exactly-once. The last delivered freshness is stored in a `_duckstring_egress` table in the same schema, updated in the same transaction as the data, so a delivery interrupted by a crash is rolled back and redone without duplicating anything.

Deliver an append Trickle or a plain table by merging it upstream. A small Ripple that reads it and writes it with `merge_table` gives it the primary key Postgres needs.

## Delivering to a DuckLake

A [DuckLake](https://ducklake.select) keeps its tables as Parquet files with their metadata in a SQL database, so DuckDB, Spark and Trino can all read them. A DuckLake Spout delivers a Pond's tables into one:

```bash
duckstring spout add reports \
  --to 'ducklake:sqlite:/srv/lake/meta.sqlite?data_path=s3://lakehouse/files/&schema=reports'
```

The destination is the string DuckDB's `ATTACH` takes for the lake, followed by Duckstring's options: `data_path` says where a new lake keeps its files (an existing lake already knows), and `schema` defaults to `main`. Files in S3 or Google Cloud Storage take the same credential parameters as an object-store Spout. For a lake catalogued in Postgres, keep the password in the secret store:

```bash
duckstring secret set LAKE_PG
duckstring spout add reports \
  --to 'ducklake:postgres:dbname=lake host=pg.internal user=loader password=${secret:LAKE_PG}?data_path=s3://lakehouse/files/'
```

Any table can go to a lake. A merge Trickle is applied by its primary key, as for Postgres, an append Trickle's new rows are inserted, and a plain table is replaced whole when its Pond publishes a new version of it. When a Spout delivers several tables, a run's tables go in one transaction, so readers of the lake see one snapshot per delivery, with `duckstring` as its author and the Pond (`reports@1`) as its commit message. Delivery is exactly-once, through a `_duckstring_egress` table in the same schema.

Duckstring doesn't compact the lake or expire its snapshots, since those are decisions for whoever owns it (DuckLake's `ducklake_merge_adjacent_files` and `ducklake_expire_snapshots` do both). It never migrates the lake's catalog either. A lake the Spout creates under DuckDB 2.0 uses DuckLake 1.1, which readers on DuckDB 1.5 can't open.

## Checking a destination

The UI's Spout form, in a Pond's **Options**, has a **Test** button, which checks the destination can be reached and the credentials work without writing any data:

<figure class="ds-figure">
  <img src="/img/docs/spout-add.webp" alt="The add-Spout form in a Pond's Options: scheme, bucket and prefix, credentials by name, table and mode, with Test and Add" width="492" height="706" />
  <figcaption>Credentials are entered as the names of environment variables or secrets, never as values.</figcaption>
</figure>

From the CLI, add the Spout and watch its first delivery:

```bash
duckstring spout ls reports
```

`ls` shows each Spout's destination, mode, last delivered freshness and state. A failed delivery shows its error there, in run history, and to any [alert channel](monitoring_and_failures.md#alerts) subscribed to `spout`. A Spout failure never affects its Pond.

A failed delivery is retried the next time the Pond publishes, up to three times, since delivery failures are often temporary: a database restarting, a network blip. If the retries are used up, the Spout stays failed until you fix the cause and reset it with `spout clear`, `spout wake` or `spout force`. Change the number of retries per Spout, addressing it as `{pond}#{spout}`:

```bash
duckstring control failure-budget 'priced#priced_line' --on-change 10
```

## Controlling when deliveries happen

A Spout never asks its Pond to run. It delivers whatever the Pond publishes, so the Pond's own Refresh decides how fresh the delivered data is.

To deliver less often than the Pond publishes, give the Spout a [window](scheduling.md#batch-sources). A Spout is addressed as `{pond}#{spout}`, and its name defaults to the table's:

```bash
# Deliver monthly_summary at most once a day, between 07:00 and 08:00 UTC
duckstring window add 'reports#monthly_summary' --name morning --every 1d --start 07:00 --duration 1h
```

This suits a destination that should change only at a fixed time, while the Pond itself stays fresh for other consumers.

Pause and resume a Spout without removing it:

```bash
duckstring spout sleep reports monthly_summary     # stop delivering
duckstring spout wake reports monthly_summary      # resume from the next publish
duckstring spout force reports monthly_summary     # resume and deliver the current output now
duckstring spout resync reports monthly_summary    # deliver everything again in full
```

Use `resync` after the destination was changed by hand or restored from a backup.

<figure class="ds-figure">
  <img src="/img/docs/spout-panel.webp" alt="A selected Spout in the web UI: its destination, table, mode and last delivery, control buttons and windows" width="320" height="490" />
  <figcaption>Selecting a Spout in the web UI shows its last delivery, the same controls and its windows.</figcaption>
</figure>
