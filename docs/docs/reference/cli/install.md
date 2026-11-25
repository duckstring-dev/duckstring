---
title: duckstring install
description: Install what Duckstring needs beyond pip.
sidebar_label: install
---

# duckstring install

```bash
duckstring install [COMPONENT...] [--yes] [--dry-run] [--postgres-version N]
```

Installs what Duckstring needs on a machine beyond `pip install`: PostgreSQL for [Records](../../guides/records_and_media.md), DuckDB's extensions, and Duckstring's extras. Only official sources are used, and the steps are printed before anything runs. With no components, shows the state of each.

| Component | What it installs |
|---|---|
| `postgres` | A PostgreSQL server, for the Catchment's own Records and for `pond run` of an App with Records. On macOS from Homebrew; on Debian and Ubuntu from the PostgreSQL project's apt repository, with the distribution's default cluster turned off; on Red Hat-family systems from its yum repository. Elsewhere, install it from postgresql.org and set `DUCKSTRING_PG_BIN` to its `bin` directory. A server that's already installed is recorded in `~/.duckstring/config.toml`, so the Catchment finds the same one. |
| `extensions [NAME...]` | DuckDB's extensions, for a machine without internet access at run time. By default the ones Duckstring loads: `httpfs`, `ducklake`, `postgres`, and `azure` when the `azure` extra is installed. Names after `extensions` are extensions, so `extensions postgres` is DuckDB's `postgres` extension; name `postgres` before `extensions` to install the server as well. |
| `aws`, `azure`, `gcp`, `databricks`, `dbt`, `lineage`, `starrocks` | Duckstring's extras, pinned to the installed Duckstring, with `uv pip install`. |

Steps on Linux that need root stop the command with the line to run instead, such as `sudo "$(which duckstring)" install postgres`.

| Option | Default | Description |
|---|---|---|
| `--yes`, `-y` | off | Run the steps without asking. |
| `--dry-run` | off | Print the steps and run nothing. |
| `--postgres-version` | `18` | The PostgreSQL major version to install. |
