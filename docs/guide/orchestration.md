# Orchestration

Duckstring orchestration uses a pull-based system modelled after Kanban. This allows the DAG to be formed at runtime, triggered by Outlets (consumers), in contrast to push-based systems that schedule at the Inlets (sources).

The unit operations (nodes) in the DAG are **Ripples**, existing in a versioned **Pond**. Often there might be only one Ripple to a Pond, but there could be many more.

## Overview

### Parents and Versioning

Each Pond defines its **Sources** (parent Ponds) and their minimum version, accepting any greater version within the same *major*. Ponds use strict semantic versioning (SemVer):

- *major*: Breaking change, e.g. schema change, table deletes, logic change
- *minor*: Non-breaking change, e.g. addition of new columns or tables, small logical changes with no potential downstream impact
- *patch*: Return to intended state, e.g. removal of an incorrect filter

A key aspect of the design is that multiple *major* versions for a given Pond could be executing concurrently - a Pond version only stops executing (and does so automatically) when it has no active Ponds depending on it downstream.

**Inlets** do not have any Sources. **Outlets** do not have any **Sinks** - Ponds listing them as Sources.

Sources can be listed as *required* or not. Any required Source will cause the Pond to wait until that source has updated at least once since the last run. If no Sources are required, the Pond will execute upon change to any Source.

### Trigger Process

Orchestration executes at the **Ripple level** — the Pond is the organisational and versioning unit, but the Ripple is the execution unit. The process below applies to each Ripple individually.

- If I have **Demand** from any Sink
    - If I have no Sources (i.e. I am an Inlet)
        - Execute
        - Clear all Demand
    - If I have any required Sources
        - If all required Sources have updated
            - Send Demand to each Source
            - Execute
            - Clear all Demand
    - If any Source has updated
        - Send Demand to each Source
        - Execute
        - Clear all Demand

Note that Demand is sent *before* execution starts, which may be counterintuitive. This allows the Sources to execute their next generation while the Pond executes, minimising latency.

### Initiating Demand

Demand is initiated at Outlets, and could be triggered by a few different mechanisms:

- *Pulse*: A single Demand is sent upstream, with no repeat. Intended to execute the DAG once.
- *Wave*: A Demand is sent upstream and a new Demand is sent whenever the Pond executes. This causes continuous operation at the maximum frequency the Pond allows, and is functionally identical to a non-Outlet Pond executing with persistent Demand.
- *Tide*: A Demand is sent upstream according to a schedule (e.g. daily). This is functionally the same as executing a Pulse periodically.

If a new Pond is attached as a Source, or if any Pond has recently upgraded its *major* version, a cold start is necessary for those Sources. In this case, the Pond will send Demand to any Source that has not previously executed, causing the Demand signal to propagate all the way upstream.

### Stops

The DAG will naturally stop if no new Demand is triggered. However, each prior level executes for one further generation before stopping, meaning a Pond *n* levels prior could execute *n* additional times unnecessarily.

Instead, a **Stop** can be emitted against an Outlet. This causes it to delete its Demand in each Source (if it exists). If any Pond has its Demand from all its Sinks (apart from when clearing that Demand itself), it also sends a Stop upstream.

Consequently, if all Outlets have Stopped, no further execution begins upstream.

A Pulse in fact sends a Stop when it begins execution, so that the upstream processes execute only once.

### Ripples

Within a Pond, Ripples execute in **push-style topological order**. When a `pond_run` starts, the runtime dispatches root Ripples (those with no intra-Pond parents) first; each Ripple dispatches its children once it completes. All Ripples within a `pond_run` share the same generation number.

Pipelining still emerges naturally. A root Ripple may only begin generation *N+1* once it has been **consumed** — all its immediate intra-Pond children have been dispatched in generation *N*. This limits pipeline depth to one generation ahead and prevents a fast root Ripple from overwriting its shared output table while a slower child is still reading it. The effective throughput bottleneck through the chain is the slowest individual Ripple, not the sum of all Ripples.

For a Pond with B1(1 unit)→B2(2 units): in steady state both run with a period of 2 units. B1 gen=*N+1* starts the moment B2 gen=*N* is dispatched (the consumed condition is satisfied), so B1 and B2 execute concurrently in a rolling pipeline.

All intra-Pond parent edges are implicitly required — there is no optional parent concept within a Pond. The optional/required distinction only applies to inter-Pond source declarations in `pond.toml`.

Intra-Pond change monitoring (watermarks) is not needed — all Ripples within a Pond share the same `pond_run` generation. Watermarks only apply at the inter-Pond boundary.

Specific detail on Ripple declaration and the `pond` handle is covered in the Ripples guide.