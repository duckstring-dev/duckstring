---
title: duckstring catchment
description: Create, register and manage Catchments.
sidebar_label: catchment
---

# duckstring catchment

Commands for creating, registering, starting and maintaining [Catchments](../../concepts/catchments.md). Registrations are stored in `~/.duckstring/config.toml`.

`-c` / `--catchment` selects a registered Catchment; see [Choosing a Catchment](index.md#choosing-a-catchment).

## Registering and starting

### `init`

```bash
duckstring catchment init --name NAME [options]
```

Creates a Catchment on this machine, registers it under `NAME`, offers to make it the default, and starts the server in the foreground. Running it again with an existing name updates the registration.

| Option | Default | Description |
|---|---|---|
| `--name`, `-n` | prompted | Name to register the Catchment under. |
| `--host` | `127.0.0.1` | Address to bind. |
| `--port`, `-p` | `7474` | Port to listen on. The web UI is served at the same address. |
| `--root` | `~/.duckstring/{name}` | Directory for the Catchment's state (its database, run ledgers and working databases). Must be a local path. |
| `--data-root` | under `--root` | Where published tables are stored: a local path or an object-store URI (`s3://`, `gs://`, `abfss://`, `/Volumes/...`). Credentials go in the URI query as `${env:NAME}`. See [Formats](../formats.md#data-root-uris). |
| `--state-backup` | none | An object-store URI or path to copy state checkpoints to, so a Catchment on a disposable machine can recover its state. |
| `--checkpoint-every` | `60s` | How often the state database is copied to `--state-backup`. |
| `--key` | none | A single full-access API key the server requires. The registration stores it so the CLI sends it. |
| `--generate-key` | off | Generate three API keys (read, demand, full), print them once, and store the full key in the registration. Can't be combined with `--key`. |
| `--header` | none | A header sent with every request to this Catchment, as `'Name: value'`. Repeatable. |
| `--yes`, `-y` | off | Make it the default Catchment without asking. |
| `--no-start` | off | Register (and generate keys) without starting the server, for when a process supervisor will run `catchment start`. |

Without `--key` or `--generate-key`, the Catchment requires no authentication.

### `start`

```bash
duckstring catchment start NAME
```

Starts the server for a Catchment registered on this machine, with the settings it was registered with.

### `connect`

```bash
duckstring catchment connect --name NAME --path URL [--key KEY] [--header 'Name: value'] [--yes]
```

Registers a Catchment running elsewhere.

| Option | Description |
|---|---|
| `--name`, `-n` | Name to register it under. |
| `--path` | The Catchment's URL. |
| `--key` | An API key to send with every request. |
| `--header` | A header to send with every request, as `'Name: value'`. Repeatable. Use it when a hosting platform authenticates requests, for example `'Authorization: Key ...'` for Posit Connect. |
| `--yes`, `-y` | Make it the default without asking. |

### `list`

```bash
duckstring catchment list
```

Lists registered Catchments and marks the default.

### `set-default`

```bash
duckstring catchment set-default NAME
```

Makes `NAME` the Catchment used when `-c` is omitted.

### `disconnect`

```bash
duckstring catchment disconnect NAME [--purge]
```

Removes a registration. For a Catchment on this machine, asks whether to delete its data directory; `--purge` deletes it without asking.

## Configuration

### `settings`

```bash
duckstring catchment settings [-c NAME] [--data-root URI]
```

Shows the Catchment's cloud configuration, including whether cloud compute is enabled. With `--data-root`, sets where published tables are stored (`s3://`, `gs://` or a shared path). The data root can only be changed before any Pond has published data. Cloud compute is enabled once the data root is remote and AWS credentials are available to the Catchment.

### `rotate-keys`

```bash
duckstring catchment rotate-keys [-c NAME] [--level LEVEL]... [--yes]
```

Replaces the Catchment's API keys and prints the new ones once. The old key for each replaced level stops working. Running Ducks are unaffected, since they use a separate internal token. Needs a full-access key in the registration, which is updated with the new full key.

| Option | Description |
|---|---|
| `--level` | `read`, `demand` or `full`. Repeatable. Defaults to all three. |
| `--yes`, `-y` | Skip the confirmation. |

## State

### `download`

```bash
duckstring catchment download [-c NAME] [--path DIR] [--yes]
```

Downloads the Catchment's state directory: its database, deployed code, run ledgers and working databases. Use it to back up a Catchment, or to carry its state across a platform redeploy. Shows the size and asks for confirmation first. Secrets are never included, nor are Pond environments, which are rebuilt when needed. When the Catchment has an external data root, the published tables are not included, since they're already stored there.

Download while nothing is running: the working databases are copied as they are.

| Option | Default | Description |
|---|---|---|
| `--path` | `./.duckstring` | Where to write the state. The default is the path a platform-hosted Catchment reads on start, so the download can go straight into a deploy bundle. |
| `--yes`, `-y` | off | Skip the size confirmation. |

### `restore`

```bash
duckstring catchment restore --from URI [--path DIR] [--yes]
```

Restores state from a `--state-backup` location into a local directory, to seed a new machine by hand. A Catchment started with a state backup configured does this automatically when its state directory is empty.

| Option | Default | Description |
|---|---|---|
| `--from` | | The backup location. |
| `--path` | `./.duckstring` | The state directory to restore into. |
| `--yes`, `-y` | off | Skip the overwrite confirmation. |

### `reset`

```bash
duckstring catchment reset [-c NAME] [--clear-history] [--yes]
```

Returns every Pond to its freshly deployed state: deletes all published data and working databases and resets freshness. Keeps deployed code, triggers, windows, Spouts, alerts, secrets and keys. Every Duck restarts.

| Option | Description |
|---|---|
| `--clear-history` | Also delete all run history. |
| `--yes`, `-y` | Skip the confirmation. |

## Ducts

A duct lets a Catchment consume Ponds from another Catchment. Each consumed Pond appears locally as a Pond Draw, which copies its data across when it changes and passes demand back upstream. Ducts are configured on the consuming Catchment, and the upstream Catchment must be registered with the CLI.

A duct holds the upstream Catchment's credentials, so only connect Catchments that trust each other fully.

### `duct create`

```bash
duckstring catchment duct create UPSTREAM [-c NAME] [--sync]
```

Creates a duct from the registered Catchment `UPSTREAM` into the consuming Catchment. `--sync` also draws every Pond the upstream exposes.

### `duct add`

```bash
duckstring catchment duct add UPSTREAM POND [-c NAME] [--major N]
```

Draws one upstream Pond over the duct. `--major` (default `1`) picks its major line.

### `duct remove`

```bash
duckstring catchment duct remove UPSTREAM POND [-c NAME] [--major N]
```

Stops drawing a Pond and removes its Pond Draw.

### `duct sync`

```bash
duckstring catchment duct sync UPSTREAM [-c NAME]
```

Draws every Pond the upstream currently exposes.

### `duct ls`

```bash
duckstring catchment duct ls [-c NAME]
```

Lists ducts and the Ponds each one draws.

### `duct destroy`

```bash
duckstring catchment duct destroy UPSTREAM [-c NAME]
```

Removes a duct and every Pond Draw it created.

### `open` and `close`

```bash
duckstring catchment open POND [-c NAME] [-m N | -v VERSION] [--tap-on-get]
duckstring catchment close POND [-c NAME] [-m N | -v VERSION]
```

Run on the upstream Catchment. `open` marks a Pond as accepting demand from other Catchments. With `--tap-on-get`, every query of the Pond through the query API (such as `duckstring query` or the data viewer) also sends it a Tap, after serving the current data. Duct transfers don't. `close` removes both.
