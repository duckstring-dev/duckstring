---
title: Running on Databricks
description: Run a Catchment as a Databricks App, with its data on a Volume, reading from and delivering to Unity Catalog.
---

# Running on Databricks

If your team's data lives in Databricks, you can run a Catchment there as a Databricks App. Databricks signs people in, gives the App its own service principal for Unity Catalog, and keeps the Catchment's data on a Volume. Your Ponds can then read Unity Catalog tables and Volume files, deliver their output back into Unity Catalog, and send their largest recomputes to a SQL warehouse.

This guide covers the setup and what to expect. [Hosting on a Platform](hosting_on_a_platform.md) explains how a hosted Catchment works in general.

## What to expect

A Databricks App is one small machine, and a Catchment runs every Pond on the machine it runs on. That suits small and medium pipelines, and it's worth knowing the limits before you start:

- Every Duck (the process that runs a Pond) runs inside the App, so the whole pipeline shares its memory. Databricks offers Medium Apps (2 vCPUs, 6 GB, the default), Large (4 vCPUs, 12 GB) and, in some regions, XLarge (12 vCPUs, 48 GB). A Medium App measured with an 8 GiB memory limit, which gives the Catchment a memory budget of about 4.9 GB, shared by four Ducks at a time by default.
- Larger recomputes can go to a SQL warehouse instead of running in the App (see [Heavy recomputes](#heavy-recomputes)).
- An App has one HTTP address behind Databricks' sign-in. The web UI, the CLI, the HTTP API, [Apps](building_apps.md) and `duckstring query` all work through it. The Postgres and Arrow Flight endpoints don't, so BI tools read the data from Databricks instead (see [Delivering to Databricks](#delivering-to-databricks)).
- Databricks Apps aren't available in every region. Check Databricks' region list for yours.

<!-- TODO(0.8.0 RC smoke test): open a Duckstring App through the Databricks App's address, WebSockets included. Untested on Databricks. -->

## Choosing a pattern

How your Inlets read from Databricks, and how the output gets back, decide whether the pipeline uses a SQL warehouse. A warehouse bills for as long as it runs, so this is the main cost decision. There are three patterns:

| | Warehouse-backed | Catalog reads | Volume reads |
|---|---|---|---|
| Inlets read | Unity Catalog tables, through a SQL warehouse | Unity Catalog tables, from their storage | Files a Databricks job writes to a Volume |
| Output goes to | Unity Catalog tables, with a `databricks://` Spout, or Lakebase | Change files a Databricks job applies, or Lakebase | Change files a Databricks job applies, or Lakebase |
| Setup | Access to a SQL warehouse and the tables (see [Granting access](#granting-access)) | External data access turned on for the metastore | Upstream jobs also write files to a Volume |
| SQL warehouse use | Every run that reads new data, and every delivery through a `databricks://` Spout | None | None |

With catalog reads or Volume reads, the Catchment uses a SQL warehouse only for a [heavy recompute](#heavy-recomputes) too big for the App, and not at all with the Flock off. BI tools that query Unity Catalog tables run on a warehouse of their own; reading from Lakebase, they don't.

### Warehouse-backed

Inlets read tables with `databricks.table`, which goes through a SQL warehouse when it can't read the table's storage, and a [`databricks://` Spout](delivering_with_spouts.md#delivering-to-databricks) writes the output back to Unity Catalog tables. It needs only ordinary grants, and nothing upstream changes.

A serverless warehouse stops once it has been idle for its auto-stop time, so with a recurring Refresh more frequent than that, it runs, and bills, the whole time. This pattern suits a pipeline that refreshes a few times a day, or a team whose warehouse runs all day anyway. With a more frequent Refresh, give the `databricks://` Spout a [window](delivering_with_spouts.md#controlling-when-deliveries-happen) so it delivers less often than the auto-stop time.

### Catalog reads

The same `databricks.table` calls read each table straight from its storage, with short-lived credentials Unity Catalog issues for it, so no warehouse runs and upstream jobs don't change. It needs two things from administrators: a metastore admin turns on External data access for the metastore, and the catalog's owner grants the App's service principal `EXTERNAL USE SCHEMA` on the schema (see [Unity Catalog tables](ingesting_data.md#unity-catalog-tables)). The tables must be Delta tables. Views, and tables whose commits Unity Catalog manages, still go through a warehouse.

<!-- TODO(0.8.0 RC smoke test): read a Unity Catalog table from its storage on a metastore with External data access on. Only the warehouse route has run against a real workspace. -->

The output leaves without a warehouse, in either or both of two ways:

- A Spout writes [change files](delivering_with_spouts.md#change-files) to a Volume, and a scheduled Databricks job applies them to Unity Catalog tables on job compute, with `duckstring` installed for its `landing` functions. [Applying them in Databricks](delivering_with_spouts.md#applying-them-in-databricks) has the job.
- A [Postgres Spout](delivering_with_spouts.md#delivering-to-postgres) keeps tables up to date in a Lakebase database, which BI tools and applications read with their Postgres connectors.

<!-- TODO(0.8.0 RC smoke test): apply change files with the documented job on job compute (the live test applied them on a warehouse). -->

### Volume reads

Upstream Databricks jobs write their output to a Volume as Delta or Parquet files, alongside or instead of a table, and Inlets read them with `databricks.files`. Writing them is ordinary Spark:

```python
orders.write.format("delta").mode("append").save("/Volumes/main/exports/feeds/order_line")
```

Reading them needs only `READ VOLUME` on that Volume, so nothing needs an administrator beyond the usual grants. Use a Volume of its own rather than the Catchment's data root. The App keeps a copy of the files it reads on its disk, so leave room for them. The output leaves as in catalog reads.

## Deploying the Catchment

The App needs three files in a workspace folder. `app.yaml` tells Databricks how to start it and configures the Catchment:

```yaml
# app.yaml
command: ["python", "start.py"]
env:
  - name: DUCKSTRING_STATE_ROOT
    value: /home/app/.duckstring
  - name: DUCKSTRING_DATA_ROOT
    value: /Volumes/main/duckstring/catchment/data
  - name: DUCKSTRING_STATE_BACKUP_URI
    value: /Volumes/main/duckstring/catchment/state
  - name: DUCKSTRING_CHECKPOINT_INTERVAL
    value: 15s
  - name: DUCKSTRING_CATCHMENT_NAME
    value: databricks
```

`start.py` starts the Catchment on the port Databricks gives the App, as one process:

```python
# start.py
import os
import sys

port = os.environ["DATABRICKS_APP_PORT"]
os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "duckstring.catchment.asgi:app",
                          "--host", "0.0.0.0", "--port", port, "--workers", "1"])
```

`requirements.txt` installs Duckstring with its Databricks support:

```text
duckstring[databricks]
```

<!-- TODO(0.8.0 RC smoke test): confirm the App installs duckstring[databricks] from PyPI. The measured App installed wheels by path. -->

Create a custom App in the workspace and deploy it from that folder. Databricks sets `DATABRICKS_HOST` and the App's service principal credentials in its environment, and Duckstring reads Volumes, tables and warehouses with them, so the Catchment needs no Databricks credentials of its own. Leave `DUCKSTRING_API_KEY` unset, since Databricks' sign-in already guards every request.

The App starts on Python 3.11 without root access. Its Ducks run on the same machine and find the Catchment by themselves, so nothing else needs configuring.

### Granting access

The App's service principal is shown in the App's settings. Someone who can grant on your catalogs gives it what the Catchment uses:

| For | Grants |
|---|---|
| The data root and state backup | `READ VOLUME` and `WRITE VOLUME` on their Volume, with `USE CATALOG` and `USE SCHEMA` |
| Reading tables | `USE CATALOG`, `USE SCHEMA` and `SELECT`; **Can use** on a SQL warehouse; `EXTERNAL USE SCHEMA` to read from storage directly |
| A `databricks://` Spout | On the target schema `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE`, `MODIFY` and `SELECT`; `READ VOLUME` and `WRITE VOLUME` on the staging Volume; **Can use** on the warehouse |
| The Flock | `READ VOLUME` and `WRITE VOLUME` on its staging Volume, with `USE CATALOG` and `USE SCHEMA`; **Can use** on the warehouse |

People who use the Catchment need **Can use** on the App itself. Everyone with it gets full access to the Catchment.

## Keeping state on a Volume

An App keeps its local disk across a redeploy but loses it when its compute stops and starts again, so the data root and the state backup above go on a Volume.

The Volume holds the durable copy of every published table, and the App's disk holds the working copy every read uses. When the Catchment starts, it restores any table whose local copy is missing or older than the Volume's, and a state directory that's empty is restored from the state backup. In a test, the [Trickle demo](../concepts/trickles.md) came back whole after the App's compute was stopped and started, and its next Refresh carried on.

The state backup copies the Catchment's database every `DUCKSTRING_CHECKPOINT_INTERVAL`, and everything else when the Catchment shuts down. Databricks gives an App 15 seconds to shut down, which may not be long enough for that last copy, hence the short interval. A Pond whose working state is then older than its published tables rebuilds it from the published tables when it next runs.

Secrets set with `duckstring secret set` aren't part of the state backup, so they're lost when the compute restarts. Keep any credentials your Ponds need in the App's environment instead, through `env` in `app.yaml`, and refer to them as `${env:NAME}`.

## Connecting the CLI

Databricks' sign-in accepts a Databricks OAuth token as a bearer token, and these expire after an hour. Register the App with a command that prints one, using `--header-command`, and the CLI runs it again before the token expires. With the Databricks CLI signed in to the workspace:

<!-- TODO(0.8.0 RC smoke test): confirm a person's `databricks auth token` is accepted by the App. Only a service principal's M2M token was measured. -->

```bash
duckstring catchment connect --name dbx \
  --path https://duckstring-1234567890123456.7.azure.databricksapps.com \
  --header-command 'Authorization: databricks auth token --host https://adb-1234567890123456.7.azuredatabricks.net' \
  --yes
```

For a service principal, the command can mint the token from its OAuth credentials:

```bash
--header-command 'Authorization: curl -s -u "$SP_CLIENT_ID:$SP_CLIENT_SECRET" https://adb-1234567890123456.7.azuredatabricks.net/oidc/v1/token -d "grant_type=client_credentials&scope=all-apis"'
```

The service principal needs **Can use** on the App. The web UI needs nothing extra: open the App's URL and sign in.

## Reading from Databricks

Ripple code reads Databricks with `duckstring.databricks`: `files` for Parquet or Delta files a Databricks job writes to a Volume, and `table` for Unity Catalog tables. Each tells you whether its input changed since the last run, and can read only the rows added since then. [Reading from Databricks](ingesting_data.md#reading-from-databricks) shows both.

`files` keeps a copy of the files on the App's disk and fetches only new ones. `table` reads a table from its storage when your metastore allows it (the `EXTERNAL USE SCHEMA` grant above), and otherwise through a SQL warehouse, set once for every Pond in `app.yaml` as `DUCKSTRING_DATABRICKS_WAREHOUSE`. Checking whether a table changed never uses the warehouse.

## Delivering to Databricks

A [Spout](delivering_with_spouts.md) delivers a Pond's tables outside the Catchment. On Databricks it's also how BI tools reach the data, since they can't connect to a Catchment running as an App. Three kinds suit Databricks:

- A [Postgres Spout](delivering_with_spouts.md#delivering-to-postgres) keeps tables up to date in a Lakebase database, Databricks' managed Postgres, without a warehouse. BI tools connect with their Postgres connectors, and applications can look rows up by key. See [Delivering to Lakebase](#delivering-to-lakebase).
- [Change files](delivering_with_spouts.md#change-files) land each delivery's changes on a Volume, and a Databricks job applies them to Unity Catalog tables on its own schedule.
- A [`databricks://` Spout](delivering_with_spouts.md#delivering-to-databricks) keeps Unity Catalog tables up to date directly, exactly once, by running statements on a SQL warehouse for every delivery. It's the simplest to set up, but a frequent delivery keeps the warehouse running (see [Warehouse-backed](#warehouse-backed)).

### Delivering to Lakebase

Lakebase signs Databricks identities in with OAuth tokens that expire after an hour, and a Spout's destination holds one fixed credential, so the Spout signs in as a Postgres role with a password instead. In the Lakebase project:

1. Turn on Postgres native login (password sign-in) in the project's settings.
2. Add a Postgres role that authenticates with a password, such as `duckstring_spout`. Lakebase generates the password; copy it.
3. As the project's owner, create a schema for the delivered tables and let the role use it:

   ```sql
   CREATE SCHEMA reporting;
   GRANT USAGE, CREATE ON SCHEMA reporting TO duckstring_spout;
   ```

Store the password as a Databricks secret, add that secret to the App as a resource, and pass it to the Catchment in `app.yaml`, where `valueFrom` names the resource's key:

```yaml
  - name: LAKEBASE_PASSWORD
    valueFrom: lakebase-password
```

<!-- TODO(0.8.0 RC smoke test): pass the password through an App secret resource with valueFrom. The live test kept it in the Catchment's secret store, which a compute restart loses. -->

Then add the Spout, with the endpoint's host from the project's connection details:

```bash
duckstring spout add priced --name lakebase \
  --to 'postgres://duckstring_spout:${env:LAKEBASE_PASSWORD}@ep-example-123.database.westeurope.azuredatabricks.net:5432/databricks_postgres?sslmode=require&schema=reporting'
```

The Spout creates its tables in the schema on its first delivery, and the role owns them. Grant `SELECT` on them to the roles your BI tools and applications sign in as.

## Heavy recomputes

When a Trickle has to recompute a table in full, for example the first time it runs, the [Flock](offloading_large_recomputes.md) can send that recompute to a SQL warehouse and bring the result back to the Duck. It's the main way to use more compute than the App's machine has. Add the warehouse and a staging Volume to `app.yaml`:

```yaml
  - name: DUCKSTRING_FLOCK_DATABRICKS_WAREHOUSE
    value: 1a2b3c4d5e6f7a8b
  - name: DUCKSTRING_FLOCK_DATABRICKS_VOLUME
    value: /Volumes/main/duckstring/flock
```

Then turn it on for the Ponds that need it:

```bash
duckstring duck set priced --flock upgrade --engine databricks
```

In `upgrade` mode a recompute goes to the warehouse only when it's too big for the App. [Offloading Large Recomputes](offloading_large_recomputes.md#databricks) covers what's sent and how results are checked.

The recompute's input travels with it: each source table is uploaded from the App to the staging Volume, and the result comes back to the App. So the Flock suits work that's too much for the App's memory over data the App already holds, such as a large join or aggregation. For data far beyond that, such as a terabyte, run the heavy transform as a Databricks job that writes a Unity Catalog table or files to a Volume, and have a Pond read its output with `table` or `files`, which pick up new rows as they arrive.
