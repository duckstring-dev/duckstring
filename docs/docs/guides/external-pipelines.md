---
title: External Pipelines as Ponds
description: Use Duckstring as the coordination layer over pipelines that run elsewhere — proxy Ponds that trigger and await an external engine.
---

# External Pipelines as Ponds

A Pond doesn't have to compute anything itself. If your transforms already run on an external engine — a warehouse job, a managed pipeline service, anything with a "start run" API — each pipeline can be wrapped as a **proxy Pond**: a single Ripple that starts the external run, polls until it finishes, and fails if it failed.

In its simplest form the data never touches the Catchment (though it can — see [moving data across the boundary](#moving-data-across-the-boundary)). What you get is the coordination layer those platforms tend to lack across pipeline boundaries:

- **Dependencies by declaration** — each domain's `pond.toml` names its upstream domains; downstream runs when upstream completes, instead of by guessed schedule offsets.
- **[Demand](triggers.md) instead of schedules** — a Tide keeps a terminal domain no staler than a bound; a Wave re-runs the chain as fast as the slowest pipeline allows; a Pulse runs everything once, in order.
- **[Fault tolerance](fault-tolerance.md) across domains** — a failed pipeline blocks its downstream domains (they never run against stale upstream state), retry budgets re-trigger it, and every attempt lands in run history with its error.
- **[Windows](windows.md)** — bound when root domains may start, e.g. to when their upstream source systems have loaded.

## The proxy Ripple

The one rule: **make it idempotent against the external system**. Duckstring's recovery machinery re-runs a Ripple that didn't complete — after a worker restart, or on a retry. A naive "always start a run" Ripple would launch a second external run alongside the first. Write it as *ensure-then-poll*: attach to an active run if one exists, start one only if none does.

A worked example against a Databricks job (the same shape fits any engine with start/status endpoints):

```python
import os
import time

import httpx

from duckstring import ripple

HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
JOB_ID = int(os.environ["SALES_JOB_ID"])
AUTH = {"Authorization": f"Bearer {os.environ['DATABRICKS_TOKEN']}"}


def _active_run() -> dict | None:
    r = httpx.get(f"{HOST}/api/2.1/jobs/runs/list",
                  params={"job_id": JOB_ID, "active_only": "true"}, headers=AUTH)
    runs = r.json().get("runs", [])
    return runs[0] if runs else None


@ripple
def run_pipeline(pond):
    run = _active_run()  # idempotent: a recovery re-run attaches instead of double-triggering
    if run is None:
        r = httpx.post(f"{HOST}/api/2.1/jobs/run-now", json={"job_id": JOB_ID}, headers=AUTH)
        run = {"run_id": r.json()["run_id"]}

    while True:
        r = httpx.get(f"{HOST}/api/2.1/jobs/runs/get",
                      params={"run_id": run["run_id"]}, headers=AUTH).json()
        state = r["state"]
        if state["life_cycle_state"] in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED"):
            break
        time.sleep(15)

    if state.get("result_state") != "SUCCESS":
        # The message lands verbatim in run history and the UI — include the link to the real logs.
        raise RuntimeError(f"{state.get('state_message', state)} — {r.get('run_page_url')}")

    # Optional: publish run metadata so `duckstring query` gives a cross-linked history.
    pond.write_table("run", pond.con.sql(
        f"SELECT {run['run_id']} AS run_id, '{r.get('run_page_url')}' AS url"
    ))
```

Long polls are fine — a Ripple may run for hours; the worker stays live throughout and the Catchment's liveness checks key off the worker, not the Ripple.

## Moving data across the boundary

Proxy Ponds don't have to be data-free. A Ripple can carry data *to* the external engine before triggering it, and carry results *back* afterwards — which inverts the usual architecture: Duckstring executes the many small transforms directly, and the external engine is reserved for the steps that genuinely need its scale. Draw the boundary so only **reduced** data crosses it — dimensions, configs, and aggregates travel; the raw volume stays where it lives.

Pushing inputs up, using a managed volume as the hand-off (file upload is plain REST; the job ingests from the volume path — `COPY INTO` or an autoloader — using the engine's own writer rather than writing its table format from outside):

```python
import tempfile
from pathlib import Path

@ripple
def push_inputs(pond):
    tiers = pond.read_table("pricing.tier")        # small: dims, thresholds, lookup tables
    with tempfile.TemporaryDirectory() as tmp:
        pq = Path(tmp) / "tier.parquet"
        tiers.write_parquet(str(pq))
        httpx.put(f"{HOST}/api/2.0/fs/files/Volumes/main/landing/in/tier.parquet",
                  content=pq.read_bytes(), params={"overwrite": "true"}, headers=AUTH)


@ripple(parents=[push_inputs])
def run_pipeline(pond):
    ...  # ensure-then-poll, as above
```

Pulling a result set down, via the SQL statement-execution API — the rows land as an ordinary Pond table, published as Parquet, and every downstream Duckstring Pond consumes it like any other Source:

```python
@ripple(parents=[run_pipeline])
def pull_summary(pond):
    r = httpx.post(f"{HOST}/api/2.0/sql/statements", headers=AUTH, json={
        "warehouse_id": os.environ["WAREHOUSE_ID"],
        "statement": "SELECT * FROM main.gold.daily_summary",
        "wait_timeout": "30s", "format": "JSON_ARRAY",
    }).json()
    cols = ", ".join(c["name"] for c in r["manifest"]["schema"]["columns"])
    rows = ", ".join(f"({', '.join(repr(v) for v in row)})" for row in r["result"]["data_array"])
    pond.write_table("daily_summary", pond.con.sql(f"SELECT * FROM (VALUES {rows}) t({cols})"))
```

(For results beyond what a single statement response should carry, have the job write Parquet to a volume and download it instead — the same Files API, in reverse.)

Stamp what crosses the boundary with [`pond.f`](incremental-ripples.md#freshness-as-the-watermark) and both sides get a shared, replay-stable watermark for free.

## Wiring the domains

Each external pipeline gets a small Pond project. Root domains are Inlets; everything else declares its upstream domains as Sources:

```toml
[pond]
name = "sales"
version = "1.0.0"
source_retries = 1        # one re-trigger when upstream refreshes after a failure

[sources]
transactions = "1.0.0"
products = "1.0.0"
```

Deploy them all, then drive the terminal domain like any other Outlet — `duckstring trigger tide reports 1d` replaces a brittle lattice of staggered schedules with one declared staleness bound.

## Practicalities

- **Dependencies and credentials live with the Catchment, not the Pond.** Deployed Pond code runs in the Catchment's Python environment — there is no per-Pond environment. Keep proxy Ripples to the standard library plus `httpx` (already installed), or add what they need to the Catchment deployment's `requirements.txt`. Likewise credentials: set them as environment variables where the Catchment runs (workers inherit them).
- **Freshness is trigger-time, not data-time.** A proxy Pond's freshness reflects when its run started — which is exactly right for ordering and staleness bounds, but the engine can't see inside the external pipeline. If a source system loads on a known cadence, say so with a [Window](windows.md) rather than expecting the freshness to discover it.
- **Retry budgets re-trigger the external run.** `immediate_retries` means "start it again right away on failure"; make sure that's acceptable for the pipeline in question before setting it above zero.
