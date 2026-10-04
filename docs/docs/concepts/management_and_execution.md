---
title: Management and Execution
description: Working With Duckstring.
---

# Management and Execution

## CLI

Everything you can do with a Catchment is available from the `duckstring` CLI, also installed as `ds`. That includes deploying Ponds, setting triggers, controlling runs and querying data. The CLI works through the Catchment's HTTP API, so scripts, CI jobs and agents can do anything a person can.

## UI

Each Catchment serves a web UI at its own address. It shows the graph of Ponds and Ripples as they run, the history of every run, and the published data. You can set triggers and control runs from the UI, but the graph itself only changes when you deploy code.

<!-- IMAGE: the web UI with the demo pipeline under a Wave, the sidebar open on a Pond, and run history below. -->

## Local Execution

A Catchment runs the same way on a laptop as on a server. You can deploy Ponds to a local Catchment, trigger them and inspect their data, knowing they will behave the same once deployed elsewhere. Moving to a shared Catchment later takes very few changes. A small pipeline could even stay on a laptop for good.

## Hosting

For a shared Catchment that is always on, run the same server on a dedicated machine. Nothing else changes. Access can be controlled with API keys at three levels: read, demand (read plus triggers) and full.

## Cloud Compute

By default, Ponds run on the Catchment's own machine. Individual Ponds can instead run on cloud compute, on AWS Fargate or EC2. This needs the Catchment's data stored in S3 and AWS credentials configured. The choice is made per Pond, in `pond.toml` or on the Catchment. Where cloud compute isn't configured, the Pond simply runs locally, so the same code works everywhere.

## Ducks and Flocks

Each running Pond gets its own worker process, called a **Duck**, which runs the Pond's Ripples with DuckDB. A Duck starts when its Pond has work and stops when the Pond goes idle. If the Catchment becomes unavailable mid-run, the Duck finishes the run and reports back once it can.

Occasionally one computation is too large for a Duck's memory, usually when a Trickle has to be recomputed in full. A Pond can allow that work to be sent to a serverless query engine, called a **Flock**, with Amazon Athena as the default. DuckDB stays the authority on the result. Only computations known to give identical results on both engines are sent, and the result is checked against the schema DuckDB would have produced. If anything goes wrong, the work runs in the Duck instead.

## Failures

When a Ripple fails, Duckstring can retry it within the same Pond Run, and can retry the whole Pond Run the next time a Source updates. Both retry budgets are set in `pond.toml` and default to zero. Once they're used up, the Pond is marked failed.

Nothing from a failed run is published, so downstream Ponds keep reading the last good output. They're marked blocked rather than failed, so a failure only shows at its root. A failed Pond recovers when a later run succeeds, when a fixed version is deployed, or when it's cleared by hand. Each failure's error and traceback appear in the run history, and failures can also be sent as alerts to a webhook or email address.

## Control

Beyond triggers, a few commands act directly on a Pond's execution. They're available from `duckstring control` and the UI.

| Command | Effect |
|---|---|
| wake | run once if the Sources already have newer data, without asking them to run |
| force | rerun now at the current freshness, even with no upstream change |
| refresh | rebuild the Pond's tables from scratch on its next run |
| repair | rebuild a connected set of Ponds now, in order |
| sleep | clear the Pond's demand and standing trigger, letting runs in progress finish |
| kill | stop the Pond's Duck immediately and hold the Pond until it's woken, forced or cleared |
| clear | reset a failed or killed Pond without running it |

## See also

- Guides: [Running on a Server](../guides/running_on_a_server.md), [Hosting on a Platform](../guides/hosting_on_a_platform.md), [Cloud Compute on AWS](../guides/cloud_compute_on_aws.md), [Monitoring and Failures](../guides/monitoring_and_failures.md)
- Reference: [CLI Overview](../reference/cli/index.md), [duckstring control](../reference/cli/control.md), [duckstring duck](../reference/cli/duck.md), [Environment Variables](../reference/environment.md), [HTTP API](../reference/http_api.md)
