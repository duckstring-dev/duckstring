---
title: duckstring alert
description: Failure and freshness notifications.
sidebar_label: alert
---

# duckstring alert

Alert channels send notifications to a webhook or email address when Ponds fail, recover or go stale. Channels are Catchment configuration and survive redeploys. All commands need full access and take `-c` / `--catchment`.

## Events

| Event | Sent when |
|---|---|
| `failure` | A Pond Run gives up after its retries, or its Duck dies or stops responding. |
| `contract` | A run's output breaks the major version's schema contract, so it isn't published. |
| `spout` | A Spout delivery fails. |
| `freshness` | A Pond has been staler than the channel's `--stale` limit. |
| `recovery` | A failed Pond or Spout recovers, or a stale Pond becomes fresh again. |

Failures are only reported for the Pond where they happened: Ponds blocked downstream aren't alerted separately, and are listed in the failure's message instead. Each failure is sent once, however many times the run is retried. A channel subscribed to `failure` also receives the matching `recovery`. Killing a Pond doesn't send a recovery.

Messages include the error but never the traceback, since a channel is an external service.

## `add`

```bash
duckstring alert add --to URI [--name NAME] [--pond POND [--major N]] [--on EVENTS] [--stale DURATION] [--renotify DURATION]
```

| Option | Default | Description |
|---|---|---|
| `--to`, `-t` | required | `https://...` or `http://...` for a webhook, or `mailto:...` for email. Credentials are written as `${env:NAME}` or `${secret:NAME}`, and a single reference can be the whole destination. See [Notification URIs](../formats.md#notification-uris). |
| `--name`, `-n` | from the scheme | The channel's name. |
| `--pond`, `-p` | every Pond | Only alert for this Pond. |
| `--major`, `-m` | the highest deployed | The major line of `--pond`. |
| `--on` | `all` | Comma-separated events from the table above, or `all`. |
| `--stale` | none | The staleness limit for `freshness` alerts, as a [duration](../formats.md#durations) such as `1h`. |
| `--renotify` | once | While a failure or staleness lasts, send it again at this interval. |

Webhook messages are JSON with a top-level `text` field, so they work with a Slack incoming webhook as well as generic receivers.

## `ls`

```bash
duckstring alert ls
```

Lists channels with their destinations, scopes and events.

## `rm`

```bash
duckstring alert rm NAME
```

Deletes a channel.

## `test`

```bash
duckstring alert test NAME
```

Sends a test message through the channel and reports whether it was delivered.

## `log`

```bash
duckstring alert log [--limit N]
```

Shows recent deliveries and their outcomes (default `50`). A delivery that keeps failing is retried a few times and then marked failed.
