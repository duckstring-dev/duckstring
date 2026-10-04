---
title: Monitoring and Failures
description: Watch pipelines, get alerted, and recover from failures.
---

# Monitoring and Failures

This guide covers watching a Catchment, what happens when something fails, how to recover, and how to be told about problems without watching. See [Failures](../concepts/management_and_execution.md#failures) for the concepts.

## Watching

`duckstring status` shows every Pond's state, freshness and standing trigger, refreshing every second:

```bash
duckstring status             # everything
duckstring status reports     # reports and everything upstream of it
duckstring status --once      # one snapshot, for scripts
```

A Pond is in one of these states, listed in order of precedence:

| State | Meaning |
|---|---|
| failed | A run gave up after its retries. |
| killed | Stopped by `control kill`; it won't run until cleared. |
| blocked | A Source it needs is failed, killed or blocked. |
| running | A Pond Run is in progress. |
| queued | It has demand and is waiting for its Sources. |
| idle | Nothing to do. |

The web UI, at the Catchment's address, shows the same on the pipeline graph. Select a Pond to see its Ripples, triggers, data and failures. The run history below the graph lists recent runs; selecting one shows the time each Ripple took, any retries, and the error and traceback of anything that failed. Tracebacks are only shown to users with full access, since they can include paths and connection details.

## What failure looks like

A run fails when a Ripple raises an exception and has no retries left, when its Duck crashes or stops responding for a minute, or when its output breaks the major version's schema contract. Nothing from a failed run is published. Downstream Ponds keep reading the last good output and are shown as blocked, rather than failed, so the failure only appears once, at its cause.

A blocked Pond doesn't request new data, but still processes anything its Sources had already produced.

A Ripple that reads a Source table that hasn't been published, because the Source hasn't produced it yet, isn't treated as failing. The Pond waits and runs again once the Source publishes.

## Retries

Two retry budgets, both 0 by default, control how hard Duckstring tries before marking a Pond failed:

```toml
[pond]
immediate_retries = 2      # retry a failed Ripple straight away, within the same run
source_retries = 3         # retry a failed run when a Source next has new data
```

Immediate retries suit transient errors, such as a flaky network call. Source retries suit problems that new data may fix, and cost nothing while waiting. With `immediate_retries = 1` and `source_retries = 1`, a Ripple that keeps failing is attempted twice in the failing run, then twice more in a run started when a Source updates, before the Pond is left failed.

`pond.toml` sets the budgets when a Pond is first deployed. After that, change them on the Catchment:

```bash
duckstring control failure-budget sales                       # show
duckstring control failure-budget sales --immediate 2 --on-change 3
```

Retried and replayed Ripples run with the same `pond.f`, so write them to be safe to repeat. Duckstring's own write methods already are.

## Recovering

Start from the failed Pond, not the blocked ones downstream: once it recovers, they unblock.

**Fix the code.** Deploy a corrected version. A redeploy clears the Pond's failure, and it runs again the next time it's needed. If it's needed now:

```bash
duckstring pond deploy
duckstring control force sales
```

**Retry without changes**, after fixing something external such as a credential or a full disk:

```bash
duckstring control force sales      # rerun now at the same freshness
duckstring control wake sales       # or: run once if the Sources have newer data
```

Both clear the failure first.

**Give up on a run** and let normal scheduling resume:

```bash
duckstring control clear sales
```

**Rebuild from scratch**, when a Pond's stored state is wrong, for example after fixing a bug that wrote bad incremental results:

```bash
duckstring control refresh sales        # the next run drops and rebuilds everything
duckstring control repair sales --downstream   # or: rebuild now, with everything downstream, in order
```

`refresh` waits for the next run; `repair` runs immediately and rebuilds each downstream Pond after its parents.

**Stop a runaway run**, one stuck or consuming too much:

```bash
duckstring control kill sales
```

A killed Pond stays stopped until it's woken, forced or cleared.

## Alerts

Alert channels notify a webhook or email address. Set up a Slack channel for failures across the Catchment:

```bash
duckstring secret set SLACK_WEBHOOK       # paste the webhook URL at the prompt
duckstring alert add --name ops --to '${secret:SLACK_WEBHOOK}' --on failure,contract,spout
duckstring alert test ops
```

The whole webhook URL is kept in the secret store, since anyone with it can post to the channel. Its value is checked when the alert is tested or first sent. Webhook messages are JSON with a `text` field, which Slack displays directly.

Alerts fire once per failure, however many retries happen, and only for the Pond that failed. Ponds blocked by it are listed in the message instead of alerting separately. A channel subscribed to `failure` also gets the recovery.

Alert when a Pond's data gets too old, for example an Outlet that should be refreshed daily:

```bash
duckstring alert add --name reports-sla --to mailto:data-team@example.com \
  --pond reports --on freshness --stale 26h
```

Email needs an SMTP server, given in the destination (`?smtp=host:587`) or as `DUCKSTRING_SMTP_HOST` on the Catchment. See [Notification URIs](../reference/formats.md#notification-uris).

By default each problem is sent once. `--renotify 6h` repeats it every six hours while it lasts. `duckstring alert log` shows recent deliveries, including ones that failed to send.

## Metrics

The Catchment serves Prometheus metrics at `/metrics`, without authentication. A scrape configuration:

```yaml
scrape_configs:
  - job_name: duckstring
    static_configs:
      - targets: ["catchment.internal:7474"]
```

Useful series include `duckstring_pond_freshness_lag_seconds` (how old each Pond's data is), `duckstring_pond_failed` and `duckstring_pond_blocked`, and `duckstring_spout_delivery_lag_seconds`. An alert on data age:

```yaml
- alert: ReportsStale
  expr: duckstring_pond_freshness_lag_seconds{pond="reports"} > 93600
```

See [Metrics](../reference/http_api.md#metrics) for the full list. Pond names appear as labels, so restrict access to the endpoint if they're sensitive.

## Tracing bad data

When a number looks wrong, find where it came from. `trace` names the run that produced some rows, and the window of Source data it read:

```bash
duckstring trace reports.monthly_summary --where "year = 2026 AND month = 9"
```

`lineage` shows which tables each Ripple actually read and wrote, and with `--columns`, which Source columns each output column came from:

```bash
duckstring lineage reports --columns
```

From there, check the Source's run history at that time, or query its data with `duckstring query`.
