---
title: duckstring secret
description: The Catchment's write-only secret store.
sidebar_label: secret
---

# duckstring secret

The secret store holds credentials for Spout and alert destinations, referenced in a destination URI as `${secret:NAME}`. It is write-only: values can be set and deleted, but never read back through the CLI, API or UI.

Secrets are stored unencrypted in a `secrets.json` file with `0600` permissions in the Catchment's state directory, and are never included in [`catchment download`](catchment.md#download). Setting a secret sends its value to the Catchment, so use HTTPS for a remote Catchment.

All commands need full access and take `-c` / `--catchment`.

## `set`

```bash
duckstring secret set NAME [--value VALUE]
```

Sets or replaces a secret. Without `--value`, the value is prompted for without echoing, which keeps it out of shell history. Names may contain letters, digits and underscores, and can't start with a digit.

## `ls`

```bash
duckstring secret ls
```

Lists secret names and when each was set.

## `rm`

```bash
duckstring secret rm NAME
```

Deletes a secret.
