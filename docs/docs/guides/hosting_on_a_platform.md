---
title: Hosting on a Platform
description: Run a Catchment on Posit Connect or another Python app host.
---

# Hosting on a Platform

Many teams already have a platform that runs Python web apps, such as Posit Connect, Databricks Apps, or a container service, with authentication handled for them. A Catchment is an ASGI web app, so it can be deployed there like any other. This guide covers the setup, and how to keep the Catchment's state across redeploys, which such platforms don't always preserve.

## The bundle

A deployable Catchment is two files:

```python
# app.py
from duckstring.catchment.asgi import app
```

```text
# requirements.txt
duckstring
```

Add any packages your Ripples import to `requirements.txt`, since Ripples run in the app's environment. Deploy the bundle as an ASGI (FastAPI) app, using the platform's usual method.

Configure the Catchment with environment variables in the platform's settings:

| Variable | Purpose |
|---|---|
| `DUCKSTRING_STATE_ROOT` | Where state is kept. Defaults to `./.duckstring`, inside the bundle directory. |
| `DUCKSTRING_DATA_ROOT` | Where published tables are stored. Use object storage on a platform; see below. |
| `DUCKSTRING_STATE_BACKUP_URI` | Where to copy state continuously, for platforms whose disk doesn't survive a redeploy. |
| `DUCKSTRING_API_KEY` | Only if the platform doesn't authenticate requests itself. |

See [Environment Variables](../reference/environment.md) for the rest.

Run exactly one process of the app. The Catchment keeps its scheduler and database in one process, and a second copy would compete with it. On Posit Connect, set "Max processes" to 1 and "Min processes" to 1, so it isn't shut down when idle.

The web UI works from whatever path the platform serves the app at, such as `/content/{id}/` on Posit Connect.

## Authentication

Let the platform control access, and leave `DUCKSTRING_API_KEY` unset. The web UI uses the platform's login session. For the CLI, register the Catchment with whatever header the platform expects. On Posit Connect, that's your Connect API key:

```bash
duckstring catchment connect --name prod \
  --path https://connect.example.com/content/1a2b3c4d/ \
  --header "Authorization: Key $CONNECT_API_KEY" --yes
```

Everyone the platform lets through gets full access to the Catchment, so restrict who can reach the app in the platform's access settings.

Ducks talk to the Catchment inside the app's own machine, on its local address, so they don't go through the platform's authentication.

## Keeping state

A Catchment's state directory holds its database, deployed Ponds, run history and configuration. The default, `./.duckstring`, sits in the app's directory, which survives restarts but is usually replaced when you redeploy the app itself, for example to upgrade Duckstring. There are two ways to keep it.

**Carry it in the bundle.** Before redeploying, download the state into the bundle directory. The download's default location is exactly where the app looks:

```bash
cd catchment-bundle
duckstring catchment download -c prod        # writes ./.duckstring
# deploy the bundle as usual
```

Download while no runs are in progress. Secrets aren't included, so set them again after redeploying.

**Back it up continuously.** Set `DUCKSTRING_STATE_BACKUP_URI` to an object-store location. The Catchment copies its database there every minute (`DUCKSTRING_CHECKPOINT_INTERVAL`), and its full state when it shuts down cleanly. A Catchment starting with an empty state directory restores from it automatically. This suits platforms with no persistent disk, such as scale-to-zero containers.

## Keeping data

Published tables are stored under the state directory by default, which makes them part of every download. On a platform, point `DUCKSTRING_DATA_ROOT` at object storage instead:

```text
DUCKSTRING_DATA_ROOT=s3://acme-lake/duckstring?region=eu-west-2
```

Tables then persist independently of the app, downloads stay small, and cloud compute becomes available (see [Cloud Compute on AWS](cloud_compute_on_aws.md)). Credentials come from the platform's AWS setup or from `${env:NAME}` references in the URI.

A data root must belong to one Catchment. A Catchment that finds another live Catchment using its data root refuses to start. If you're sure the other one is gone, for example an old deployment that wasn't shut down cleanly, set `DUCKSTRING_FORCE_TAKEOVER=1` once to claim it.
