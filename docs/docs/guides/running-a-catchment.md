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
| `--yes`, `-y` | | Set as default without prompting |

Once created, start it again any time with:

```bash
duckstring catchment start dev
```

The server is fully restartable: state lives on disk, not in the process (see [Restart behaviour](#restart-behaviour)).

## Connect to a remote Catchment

A Catchment running elsewhere is registered by URL:

```bash
duckstring catchment connect --name prod --path https://catchment.example.com
```

From then on `prod` works exactly like a local Catchment in every command. Local-vs-remote is a property of where the server runs, not of how you use it — start local, move to a hosted server later, and your commands don't change.

## Managing registrations

Registrations live in `~/.duckstring/config.toml` and are managed with:

```bash
duckstring catchment list                 # all registered Catchments (● marks the default)
duckstring catchment set-default prod    # change the default
duckstring catchment disconnect dev      # unregister (offers to delete local data; --purge skips the prompt)
```

Every command that talks to a Catchment accepts `--catchment`/`-c {name}`; without it, the default is used (and if exactly one Catchment is registered, it's implicitly the default).

## What's in the root directory

The `--root` directory is the Catchment's entire state:

```text
~/.duckstring/dev/
├── duck.db                      # the Catchment database: graph, freshness, triggers, run history
└── ponds/
    └── sales/
        ├── 1.0.0/               # each deployed version's source, as uploaded
        ├── registry.duckdb      # the Pond's live working database
        ├── data/                # exported Parquet snapshots — the published output
        │   └── sale_line.parquet
        └── pond.db              # the Pond worker's run ledger
```

Back up the root and you've backed up the Catchment. Paths inside the database are relative to the root, so the directory is relocatable.

## Monitoring

```bash
duckstring status            # live view of every active Pond
duckstring status sales      # one Pond and its upstream lineage
duckstring status --once     # single snapshot, no live updates
```

The live view polls the Catchment and shows each Pond's state (idle / queued / running / failed / killed / blocked), freshness, and standing trigger. It exits when everything settles unless `--watch` is set; `--all`/`-a` includes inactive Ponds. The [web UI](web-ui.md) at the Catchment's URL shows the same state graphically.

## Restart behaviour

The Catchment is designed to be stopped and started without ceremony:

- **State restores from disk.** On startup it rebuilds the engine state — freshness, demand, triggers, windows, failure states — from its database.
- **Interrupted runs resume.** Pond Runs that were in flight are re-dispatched; each Pond's worker reconciles against its own ledger and re-runs only the Ripples that hadn't completed.
- **Workers tolerate the gap.** Worker processes survive Catchment downtime: they finish their in-flight runs independently, buffer their progress events, and replay them (idempotently) when the Catchment returns.

The practical upshot: restarting the Catchment mid-pipeline loses nothing and re-computes almost nothing. Details in [Architecture](../reference/architecture.md).

## Hosted Catchments

There are future plans for a managed Catchment service at [duckstring.com](https://duckstring.com) — if you're interested, [get in touch](mailto:dev@duckstring.com).
