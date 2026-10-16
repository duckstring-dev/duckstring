---
title: duckstring window
description: Declare when an Inlet can produce new data.
sidebar_label: window
---

# duckstring window

Windows declare when an Inlet can produce new data. The Pond runs at most once per window, never between windows, and its data counts as fresh until the window ends. See [Windows](../../concepts/orchestration.md#windows).

Windows are Catchment configuration and survive redeploys. A window can also be set on a Spout, as `{pond}#{spout}`, to limit how often it delivers.

Every command takes `-c` / `--catchment`, `-m` / `--major` and `-v` / `--version`; see [CLI Overview](index.md#choosing-a-major-version).

## `window add`

```bash
duckstring window add POND --name NAME --every INTERVAL [options]
```

| Option | Default | Description |
|---|---|---|
| `--name`, `-n` | required | A name for the window, unique on this Pond. |
| `--every`, `-e` | required | How often windows open, as a single-unit [duration](../formats.md#durations) such as `1d`, `12h` or `10s`. |
| `--start`, `-s` | 00:00 today (UTC) | When the first window opens: ISO 8601, or `HH:MM` for today in UTC. |
| `--duration`, `-d` | `--every` | How long each window stays open. Omitting it gives back-to-back windows. |
| `--on`, `-o` | every day | Only open on these weekdays, such as `MON,WED,FRI`. |
| `--until`, `-u` | none | When the windows stop, in ISO 8601. |

Windows on one Pond can't overlap; an overlapping window is rejected.

```bash
# A nightly export that lands between 02:00 and 03:00 UTC
duckstring window add transactions --name nightly --every 1d --start 02:00 --duration 1h
```

## `window ls`

```bash
duckstring window ls POND
```

Lists the Pond's windows.

## `window rm`

```bash
duckstring window rm POND NAME
```

Removes a window.
