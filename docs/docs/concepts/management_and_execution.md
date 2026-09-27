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
