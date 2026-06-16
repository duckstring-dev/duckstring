---
title: Running a Catchment
description: Start, connect to, and operate a Catchment.
---

# Running a Catchment

Everything else — deploying, triggering, querying — needs a [Catchment](../concepts/catchment.md) to talk to. This guide covers creating one locally, connecting to remote ones, and what operating it day-to-day looks like.

## Create a local Catchment

```bash
duckstring catchment init --name dev
```

This creates a Catchment named `dev`, registers it in your CLI config, offers to set it as the default, and starts the server in the foreground (`Ctrl+C` stops it). Defaults and their flags:

| Option | Default | Meaning |
|---|---|---|
| `--name`, `-n` | *(prompted)* | The name the Catchment is registered under |
| `--host` | `127.0.0.1` | Bind address |
| `--port`, `-p` | `7474` | Port — the web UI and API are served here |
| `--root` | `~/.duckstring/{name}` | Where the Catchment's data lives |
| `--key` | *(none — open)* | API key the server requires on every request; the CLI stores it and sends it automatically |
| `--yes`, `-y` | | Set as default without prompting |

Once created, start it again any time with:

```bash
duckstring catchment start dev
```

The server is fully restartable: state lives on disk, not in the process (see [Restart behaviour](#restart-behaviour)).

## Connect to a remote Catchment

A Catchment running elsewhere is registered by URL:

```bash
duckstring catchment connect --name prod --path https://catchment.example.com --key $PROD_KEY
```

`--key` is the server's API key (if it requires one); it is stored against the registration and attached to every request — including by the Ducks the server spawns. From then on `prod` works exactly like a local Catchment in every command. Local-vs-remote is a property of where the server runs, not of how you use it — start local, move to a hosted server later, and your commands don't change.

## Managing registrations

Registrations live in `~/.duckstring/config.toml` and are managed with:

```bash
duckstring catchment list                 # all registered Catchments (● marks the default)
duckstring catchment set-default prod    # change the default
duckstring catchment disconnect dev      # unregister (offers to delete local data; --purge skips the prompt)
```

Every command that talks to a Catchment accepts `--catchment`/`-c {name}`; without it, the default is used (and if exactly one Catchment is registered, it's implicitly the default).

## Authentication

A Catchment is open by default — fine on `127.0.0.1`. Beyond your machine there are two models, and which one applies depends on where the Catchment runs.

### Platform auth (hosted deployments)

If the Catchment runs behind a service that already gates requests — Posit Connect, an oauth2 proxy, a cloud platform's IAM — leave the Catchment itself open and let the platform do the auth. This works without configuration on two of the three surfaces:

- **The web UI** is same-origin, so the platform's login session (cookies) flows with every request automatically.
- **Ducks** are subprocesses next to the server, dialing it directly inside the sandbox — they never pass through the platform's gate.

Only the **CLI** enters through the front door, so it must present the platform's credential. Register it as custom headers attached to every request:

```bash
duckstring catchment connect --name prod --path https://connect.example.com/content/abc123/ \
    --header "Authorization: Key $POSIT_API_KEY"
```

`--header` is repeatable and takes any `'Name: value'` pair, so it covers whatever the platform in front expects. The UI also works when the Catchment is hosted **under a path prefix** (like Posit Connect's `/content/{guid}/`) — all of its asset and API references are relative.

### Built-in API key (self-hosted)

To expose a bare Catchment (a VM, a LAN box) without a platform in front, give it an API key:

```bash
duckstring catchment init --name prod --host 0.0.0.0 --generate-key
```

`--generate-key` creates a fresh key, prints it once, and stores it against the registration so `catchment start prod` reuses it; pass `--key` instead to supply your own (the two are mutually exclusive), or set `DUCKSTRING_API_KEY` in the server's environment. With a key set, every `/api` request except the health check must carry `Authorization: Bearer {key}` and is rejected `401` otherwise. Clients register the key once with `catchment connect --key`; the server's own Ducks inherit it automatically; the web UI prompts for it on first visit and keeps it in the browser.

Transport security is yours to provide — put a keyed Catchment behind TLS (a reverse proxy) before sending the key over a network. Either way, keys and headers live in `~/.duckstring/config.toml`, which the CLI keeps private (`0600`).

## Hosting on a platform

Anywhere that runs an ASGI app can host a Catchment. The packaged entry is `duckstring.catchment.asgi:app`, configured entirely by environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DUCKSTRING_ROOT` | `./.duckstring` | The Catchment root. The default is relative to the working directory; point it at a persistent path for durable state. |
| `DUCKSTRING_API_KEY` | *(unset)* | The built-in API key. Leave unset when the platform already gates requests (see [Authentication](#authentication)). |
| `DUCKSTRING_CATCHMENT_URL` | *(unset)* | The Duck dial-back address. Normally unset: the Catchment learns its bound address from the first request it serves, and its Ducks dial that directly. |
| `DUCKSTRING_DATA_PLANE` | `iceberg` | How Ponds publish their tables for each other — `iceberg` (default; snapshots + schema metadata) or `parquet` (whole-table snapshots, the lightest/offline opt-out). See [the data plane](#the-data-plane). |

One rule applies everywhere: **run exactly one process of the app.** The Catchment is a single brain — one scheduler, one database, one set of Ducks. Multiple workers (a `--workers` flag, a platform's process autoscaling) would double-dispatch runs.

### A server or container

The simplest hosted Catchment is uvicorn behind whatever TLS proxy you already run (Caddy, nginx, Traefik):

```bash
DUCKSTRING_ROOT=/var/lib/duckstring DUCKSTRING_API_KEY=$KEY \
    uvicorn duckstring.catchment.asgi:app --host 127.0.0.1 --port 7474
```

or containerised, with the root on a volume:

```dockerfile
FROM python:3.13-slim
RUN pip install duckstring
ENV DUCKSTRING_ROOT=/data
VOLUME /data
EXPOSE 7474
CMD ["uvicorn", "duckstring.catchment.asgi:app", "--host", "0.0.0.0", "--port", "7474"]
```

```bash
docker run -p 7474:7474 -v duckstring-data:/data -e DUCKSTRING_API_KEY=$KEY my-catchment
```

Both of these use the built-in key; clients connect with `catchment connect --key`.

### A gated app platform (e.g. Posit Connect)

Platforms that host ASGI apps behind their own login — Posit Connect, an oauth2-proxied PaaS — need only a two-file bundle:

```text
catchment-deploy/
├── app.py              # from duckstring.catchment.asgi import app
└── requirements.txt    # duckstring
```

For Posit Connect specifically: `rsconnect deploy fastapi . --title "Duckstring Catchment"`, then in the content settings set **Max processes = 1**, **Min processes = 1** (standing Waves/Tides need the scheduler ticking between visits), and a generous idle timeout. Leave `DUCKSTRING_API_KEY` unset — the platform's gate is the auth: the UI works through its login (and under the content's path prefix), and the CLI connects with the platform credential as a header, e.g. `--header "Authorization: Key $POSIT_API_KEY"`.

### Surviving a redeploy of the Catchment app

Platforms like Connect **replace the content directory on every redeploy**, and the default root lives inside it — so a redeploy of the Catchment app wipes deployed Ponds, history, and data (Pond deploys and triggers are unaffected; this is only about redeploying the Catchment itself). The defaults are arranged so state can ride along in the bundle:

```bash
cd catchment-deploy/
duckstring catchment download -c prod      # pulls the root into ./.duckstring (after a size confirmation)
rsconnect deploy fastapi .                 # redeploy WITH the state in the bundle
```

The new deployment starts from exactly the downloaded state. `catchment download` streams the whole root with consistent SQLite snapshots; do it while the Catchment is quiescent (no runs in flight) so the DuckDB registries are coherent too. It doubles as a plain backup.

## What's in the root directory

The `--root` directory is the Catchment's entire state:

```text
~/.duckstring/dev/
├── duck.db                      # the Catchment database: graph, freshness, triggers, run history
└── ponds/
    └── sales/
        ├── 1.0.0/               # each deployed version's source, as uploaded
        └── m1/                  # runtime state of major line 1 (m2/ if a 2.x is live, …)
            ├── registry.duckdb  # the line's live working database
            ├── data/            # exported Parquet snapshots — the published output
            │   └── sale_line.parquet
            └── pond.db          # the line's worker run ledger
```

Back up the root and you've backed up the Catchment. Paths inside the database are relative to the root, so the directory is relocatable.

## The data plane

A Pond publishes its output tables into its line's `data/` directory; Sinks and [queries](querying-data.md) read from there. How that publishing happens is the **data plane**, set by `DUCKSTRING_DATA_PLANE`:

- **`iceberg`** (default) — an [Apache Iceberg](https://iceberg.apache.org/) base layer (a per-line catalog `catalog.json` + table metadata, **over Parquet data files** — it's a metadata/snapshot layer, not a file-format change). Each run is an overwrite commit recorded as a snapshot stamped with the run's [freshness](../concepts/freshness.md). It ships with Duckstring (`pyiceberg`; a lightweight file-backed catalog means **no SQLAlchemy**). DuckDB's `iceberg` extension is loaded on first read (a one-time download — so a fully offline Catchment should either pre-cache it or use `parquet`). A flat `{table}.parquet` copy is still written alongside, so [ducts](connecting-catchments.md) and direct downloads are unchanged.
- **`parquet`** — each table is one `{table}.parquet` file, overwritten wholesale per run. The lightest option (no catalog, no extension); the right choice for an offline Catchment or when you don't need snapshots.

Both are **behaviour-neutral** — same overwrite-per-run semantics, same query results. Iceberg is the default because it adds the snapshots and schema metadata that version contracts and future incremental reads build on. The whole root — catalog included — is captured by `catchment download` (download while [quiescent](#surviving-a-redeploy-of-the-catchment-app)).

The `_duckstring_*` column-name prefix is **reserved** for framework system columns on either plane; a published table using it is rejected at write.

## Monitoring

```bash
duckstring status            # live view of every active Pond
duckstring status sales      # one Pond and its upstream lineage
duckstring status --once     # single snapshot, no live updates
```

The live view polls the Catchment and shows each Pond's state (idle / queued / running / failed / killed / blocked), freshness, and standing trigger, staying open until `Ctrl+C`. The [web UI](web-ui.md) at the Catchment's URL shows the same state graphically.

## Restart behaviour

The Catchment is designed to be stopped and started without ceremony:

- **State restores from disk.** On startup it rebuilds the engine state — freshness, demand, triggers, windows, failure states — from its database.
- **Interrupted runs resume.** Pond Runs that were in flight are re-dispatched; each Pond's worker reconciles against its own ledger and re-runs only the Ripples that hadn't completed.
- **Workers tolerate the gap.** Worker processes survive Catchment downtime: they finish their in-flight runs independently, buffer their progress events, and replay them (idempotently) when the Catchment returns.

The practical upshot: restarting the Catchment mid-pipeline loses nothing and re-computes almost nothing. Details in [Architecture](../reference/architecture.md).

## Hosted Catchments

There are future plans for a managed Catchment service at [duckstring.com](https://duckstring.com) — if you're interested, [get in touch](mailto:dev@duckstring.com).
