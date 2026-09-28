---
title: Delivering with Spouts
description: Deliver a Pond's tables to object storage or Postgres.
---

# Delivering with Spouts

Some consumers can't query a Catchment: an application reading its own Postgres database, a partner collecting files from a bucket. A Spout delivers a Pond's tables to a destination like these whenever the Pond publishes new output. This guide sets up the common destinations. See [`duckstring spout`](../reference/cli/spout.md) for every option.

## Delivering files

Write a Pond's tables as Parquet files into a directory:

```bash
duckstring spout add reports --to file:///srv/exports/reports
```

Each table is written to `{directory}/{table}.parquet` and replaced whenever `reports` publishes. Deliver one table rather than all of them with `--table monthly_summary`.

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

## Checking a destination

The UI's Spout form has a **Test** button, which checks the destination can be reached and the credentials work without writing any data. From the CLI, add the Spout and watch its first delivery:

```bash
duckstring spout ls reports
```

`ls` shows each Spout's destination, mode, last delivered freshness and state. A failed delivery shows its error there, in run history, and to any [alert channel](monitoring_and_failures.md#alerts) subscribed to `spout`. A Spout failure never affects its Pond.

## Controlling when deliveries happen

A Spout never asks its Pond to run. It delivers whatever the Pond publishes, so the Pond's own trigger decides how fresh the delivered data is.

To deliver less often than the Pond publishes, give the Spout a [window](scheduling.md#batch-sources). A Spout is addressed as `{pond}#{spout}`, and its name defaults to the table's:

```bash
# Deliver monthly_summary at most once a day, between 07:00 and 08:00 UTC
duckstring trigger window 'reports#monthly_summary' add --name morning --every 1d --start 07:00 --duration 1h
```

This suits a destination that should change only at a fixed time, while the Pond itself stays fresh for other consumers.

Pause and resume a Spout without removing it:

```bash
duckstring spout sleep reports monthly_summary     # stop delivering
duckstring spout wake reports monthly_summary      # resume from the next publish
duckstring spout force reports monthly_summary     # resume and deliver the current output now
duckstring spout resync reports monthly_summary    # deliver everything again in full
```

Use `resync` after the destination was changed by hand or restored from a backup. A failed Spout stays failed until you fix the cause and reset it with `spout clear`, `spout wake` or `spout force`.
