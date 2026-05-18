# Orchestration

Duckstring orchestration uses a pull-based system modelled after Kanban. This allows the DAG to be formed at runtime, triggered by Outlets (consumers), in contrast to push-based systems that schedule at the Inlets (sources).

The unit operations (nodes) in the DAG are **Ripples**, existing in a versioned **Pond**. Often there might be only one Ripple to a Pond, but there could be many more.

## Basic Summary

### Parents and Versioning

Each Pond defines its **Sources** (parent Ponds) and their minimum version, accepting any greater version within the same *major*. Ponds use strict semantic versioning (SemVer):

- *major*: Breaking change, e.g. schema change, table deletes, logic change
- *minor*: Non-breaking change, e.g. addition of new columns or tables, small logical changes with no potential downstream impact
- *patch*: Return to intended state, e.g. removal of an incorrect filter

A key aspect of the design is that multiple *major* versions for a given Pond could be executing concurrently - a Pond version only stops executing (and does so automatically) when it has no active Ponds depending on it downstream.

**Inlets** do not have any Sources. **Outlets** do not have any **Sinks** - Ponds listing them as Sources.

Sources can be listed as *required* or not. Any required Source will cause the Pond to wait until that source has updated at least once since the last run. If no Sources are required, the Pond will execute upon change to any Source.

### Trigger Process

When the state of a Pond or its Sources changes, the Pond follows the process:

- If I have Demand from any Sink
-- If I have no Sources (i.e. I am an Inlet)
--- Execute
-- If I have any required Sources
--- If all required Sources have updated
---- Send Demand to each Source
---- Execute
-- If any Source has updated
--- Send Demand to each Source
--- Execute

Note that Demand is sent *before* execution starts, which may be counterintuitive. This allows the Sources to execute their next generation while the Pond executes, minimising latency.

Executing also clears Demand from all Sinks.

### Initiating Demand

Demand is initiated at Outlets, and could be triggered by a few different mechanisms:

- *Pulse*: A single Demand is sent upstream, with no repeat. Intended to execute the DAG once.
- *Wave*: A Demand is sent upstream and a new Demand is sent whenever the Pond executes. This causes continuous operation at the maximum frequency the Pond allows, and is functionally identical to a non-Outlet Pond executing with persistent Demand.
- *Tide*: A Demand is sent upstream according to a schedule (e.g. daily). This is functionally the same as executing a Pulse periodically.

If a new Pond is attached as a Source, or if any Pond has recently upgraded its *major* version, a cold start is necessary for those Sources. In this case, the Pond will send Demand to any Source that has not previously executed, causing the Demand signal to propagate all the way upstream.

### Stops

