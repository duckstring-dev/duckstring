import type {
  PondId,
  RippleId,
  SinkId,
  Ripple,
  RippleRunState,
  DemandRecord,
  WatermarkMap,
} from './types';
import { getRoots, getLeaves } from './graph';

// General Approach:
// - Kanban-style pull-based demand system managed at the level of Ripples
// - A Ripple being in a Pond acts as though there is a 'start' Ripple prior to all roots in the Pond, and an 'end' Ripple after all leaves in the Pond, both with duration 0
// - Any immediate-propagation conditions like on cold starts (transition from stopped state) or stops can be abstracted to encapsulate the entire Pond
// - Consequently, the 'stopped' state exists at the Pond level, with no need to track against Ripples
// - To satisfy the case that Pulse only occurs if all demand is Pulse, if isPulse=false it will remain so until it is cleared by a run
// -- This means that if a Wave demand is received it will persist until the run completes, even if all sinks later replace with Pulse demand
// -- As demand should only be sent when a downstream Pond starts, which should only be when this Pond completes, demand should in any case only be received once per sink

// Pond Sentinel pseudocode:
/*
-- Handle demand -- 
On change to self.demand:
  If self.demand == 'stop':
    Set self.isStopped=true
    For each source:
      Set source.demand='stop' // Stops propagate upstream immediately

  If self.demand == 'wave':
    Set self.isWave=true

  If self.isStopped:
    Set self.isStopped=false // Any demand immediately wakes a stopped Pond
    For each ripple in this pond: // Cold starts trigger demand on all ripples immediately (emulates propagate-if-stopped behavior of Ponds)
      Set ripple.isWave=self.isWave
      Set ripple.hasDemand=true
  Else:
    For each leaf ripple in this pond: // Normal starts trigger demand only on leaf ripples
      Set ripple.isWave=self.isWave
      Set ripple.hasDemand=true

  For each source where source.isStopped=true:
    Set source.demand=self.demand // Stopped sources immediately receive demand

-- Handle generations --
On change to any ripples.generation:
  Set self.generation_started to max(ripples.generation_started)
  Set self.generation_completed to min(ripples.generation_completed)
  If self.generation_started > self.generation_completed:
    Set self.isRunning=true // Mostly for state tracking
  Else:
    Set self.isRunning=false

-- Handle starts --
// Clicking 'start' on a Pond sets self.hasDemand=true directly
// Otherwise, self.hasDemand is set by root ripples
// Root ripple starts are driven by self.isReady
On change to self.hasDemand or any source.generation_completed or self.isStopped:
  self.isReady=false
  if !self.isStopped:
    If self.hasDemand:
      -- Propagate to stopped sources --
      For each source where source.isStopped=true:
        If self.isWave:
          Set source.demand='wave'
        Else:
          Set source.demand='pulse'

      -- Check for changes --
      If there are no sources (inlet):
        self.isReady=true
      Else if there are no isRequired sources (execute on any change):
        If any source has updated (generation > watermark):
          self.isReady=true
      Else if all isRequired sources have updated (generation > watermark):
        self.isReady=true

-- Set state --
// Mostly for use in UI, not logic
On change to self.isStopped or self.isReady or self.hasDemand or self.isRunning:
  If self.isStopped:
    Set self.state='stopped'
  Else if self.isRunning:
    Set self.state='running'
  Else if self.isReady:
    Set self.state='ready'
  Else if self.hasDemand:
    Set self.state='queued'
  Else:
    Set self.state='idle'
*/

// Ripple Sentinel pseudocode:
/*
-- Handle starts --
On change to self.hasDemand or any parent.generation_completed or self.isRunning:
  self.isReady=false

  -- Check for completion --
  // Ripples always execute if they have parents with a higher generation to ensure any started pond run completes
  If there are no parents (root):
    If pond.isReady:
      self.isReady=true
  Else if all parents.generation_completed > self.generation_started:
    self.isReady=true

  -- Start run --
  if self.isReady and !self.isRunning:
    Set self.isRunning=true
    Set self.generation_started to min(parents.generation_completed)
    If self.isWave:
      If !pond.isStopped: // Wave is demoted to Pulse if the pond is stopped to clear queue
        For each parent:
          Set parent.hasDemand=true
          Set parent.isWave=true
    Set self.hasDemand=false // Clear demand on start
    Set self.isWave=false // Clear wave mode to allow pulse demotion on next run
    Start task

-- Handle completions --
On task completion:
  Set self.isRunning=false
  Set self.generation_completed=self.generation_started

-- Set state --
// Mostly for use in UI, not logic
On change to self.isReady or self.hasDemand or self.isRunning or pond.isStopped:
  Else if self.isRunning:
    Set self.state='running'
  Else if self.isReady:
    Set self.state='ready' // This state should never be seen, as the ripple will immediately run if it is not running and ready
  Else if self.hasDemand:
    Set self.state='queued'
  Else if pond.isStopped:
    Set self.state='stopped'
  Else:
    Set self.state='idle'
*/


// ---------------------
// Pond Sentinel pseudocode:
/*
-- Handle demand -- 
If any demand:
  -- Check for stops --
  If all demand has isStop=true:
    Send isStop demand to all sources
    Set self.isStopped=true (enter stop state)
    Clear demand
    Exit

  -- Determine if in Pulse or Wave mode --
  If all demand isPulse where !isStop:
    Set self.isPulse=true
  Else:
    Set self.isPulse=false

  -- Immediate demand propagation --
  For each source with isStopped=true:
    Send demand to source: (sinkId=self.id, isPulse=self.isPulse, isStop=false)

  -- Become active --
  If self.isStopped:
    Set self.isTriggered=true (cold start)
    Set self.isStopped=false

-- Check for changes --
self.isReady=false
If there are no sources (inlet):
  self.isReady=true
Else if there are no isRequired sources (execute on any change):
  If any source has updated (generation > watermark):
    self.isReady=true
Else if all isRequired sources have updated (generation > watermark):
  self.isReady=true

-- Check for triggers --
If self.isTriggered (set by root Ripples):
  If isReady:
    -- Start run --
    Increment self.generation_started
    Update watermarks for all sources (watermark=source.generation_completed)
    For all Ripples in this Pond:
      Create a Ripple Task for that Ripple with generation=self.generation_started and isPulse=self.isPulse
*/

// Ripple Task pseudocode:
/*
-- Determine Ripple type --
If no parents:
  self.isRoot=true
If no children:
  self.isLeaf=true

-- Check for parent completion --
self.isReady=false
If self.isRoot:
  self.isReady=true
Else if all parents (in this generation) have isCompleted=true:
  self.isReady=true

-- Run
If !self.isRunning and self.isReady:
  Start run

-- Event handling --
On run start:
  Set self.isRunning=true

  -- Trigger a new Pond run --
  If self.isRoot:
    If !self.isPulse:
      Set pond.isTriggered=true
  
  -- Clear Pond demand --
  If self.isLeaf:
    Clear all demand records for owning Pond

On run complete:
  Set self.isRunning=false
  Set self.isCompleted=true

  -- Update generation_completed --
  If all Ripple Tasks in this generation have completed:
    Set pond.generation_completed=self.generation
*/


export interface OrchestrState {
  ponds: Record<PondId, { id: PondId; name: string; sources: PondId[] }>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  watermarks: WatermarkMap;
  triggers: Record<PondId, { pondId: PondId; kind: 'wave' | 'tide'; periodMs?: number }>;
}

function wmKey(sourceId: RippleId, sinkId: RippleId): string {
  return `${sourceId}::${sinkId}`;
}

function getWatermark(watermarks: WatermarkMap, sourceId: RippleId, sinkId: RippleId): number {
  return watermarks[wmKey(sourceId, sinkId)] ?? 0;
}

function upsertDemand(
  demand: DemandRecord[],
  sinkId: SinkId,
  isStop: boolean,
  isPersistent: boolean
): DemandRecord[] {
  const existing = demand.findIndex((d) => d.sinkId === sinkId);
  const record: DemandRecord = { sinkId, isStop, isPersistent };
  if (existing === -1) return [...demand, record];
  const next = [...demand];
  next[existing] = record;
  return next;
}

function removeDemandBySink(demand: DemandRecord[], sinkId: SinkId): DemandRecord[] {
  return demand.filter((d) => d.sinkId !== sinkId);
}

function isActive(demand: DemandRecord[]): boolean {
  return demand.some((d) => !d.isStop);
}

function isIdle(demand: DemandRecord[]): boolean {
  return demand.length === 0;
}

function isStopped(demand: DemandRecord[]): boolean {
  return demand.length > 0 && demand.every((d) => d.isStop);
}

function isRoot(ripple: Ripple, ripples: Record<RippleId, Ripple>): boolean {
  return !ripple.parents.some((pid) => ripples[pid]?.pondId === ripple.pondId);
}

export function sourcesReady(
  rippleId: RippleId,
  state: OrchestrState
): boolean {
  const ripple = state.ripples[rippleId];
  if (!ripple) return false;

  if (!isRoot(ripple, state.ripples)) {
    // Non-root: all intra-pond parents must have generation > 0 and > watermark
    for (const pid of ripple.parents) {
      const parent = state.ripples[pid];
      if (!parent || parent.pondId !== ripple.pondId) continue;
      const parentState = state.rippleStates[pid];
      if (!parentState) return false;
      if (parentState.generation === 0) return false;
      if (parentState.generation <= getWatermark(state.watermarks, pid, rippleId)) return false;
    }
    return true;
  }

  // Root ripple: check inter-pond sources
  const pond = state.ponds[ripple.pondId];
  if (!pond || pond.sources.length === 0) return true;

  for (const sourcePondId of pond.sources) {
    const leaves = getLeaves(sourcePondId, state.ripples);
    for (const leaf of leaves) {
      const leafState = state.rippleStates[leaf.id];
      if (!leafState) return false;
      if (leafState.generation === 0) return false;
      if (leafState.generation <= getWatermark(state.watermarks, leaf.id, rippleId)) return false;
    }
  }
  return true;
}

export function canRippleStart(
  rippleId: RippleId,
  state: OrchestrState
): { yes: boolean; pulseMode: boolean } {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple) return { yes: false, pulseMode: false };
  if (rs.isRunning) return { yes: false, pulseMode: false };

  const ready = sourcesReady(rippleId, state);

  if (isActive(rs.demand) && ready) return { yes: true, pulseMode: false };

  // Pulse-mode exception: idle non-root ripple whose sources have new data
  if (isIdle(rs.demand) && !isRoot(ripple, state.ripples) && ready) {
    return { yes: true, pulseMode: true };
  }

  return { yes: false, pulseMode: false };
}

function propagateDemandToRipple(
  targetId: RippleId,
  sinkId: SinkId,
  isStop: boolean,
  isPersistent: boolean,
  state: OrchestrState
): OrchestrState {
  const targetRs = state.rippleStates[targetId];
  if (!targetRs) return state;

  // Cold-start guard: if target is idle or stopped, force pulse
  const effectivePersistent = (isIdle(targetRs.demand) || isStopped(targetRs.demand))
    ? false
    : isPersistent;

  const newDemand = upsertDemand(targetRs.demand, sinkId, isStop, effectivePersistent);
  let newState: OrchestrState = {
    ...state,
    rippleStates: {
      ...state.rippleStates,
      [targetId]: { ...targetRs, demand: newDemand },
    },
  };

  if (isStop) {
    newState = maybeEagerStop(targetId, newState);
  }

  return newState;
}

// Propagate active demand upstream for a ripple that is blocked (sources not ready).
// This ensures cold-start chains kick off without waiting for the blocked ripple to start.
function propagateActiveUpstream(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple) return state;

  const isPersistent = rs.demand.some((d) => !d.isStop && d.isPersistent);
  let newState = state;

  if (!isRoot(ripple, state.ripples)) {
    for (const pid of ripple.parents) {
      const parent = state.ripples[pid];
      if (!parent || parent.pondId !== ripple.pondId) continue;
      newState = propagateDemandToRipple(pid, rippleId, false, isPersistent, newState);
    }
  } else {
    const pond = state.ponds[ripple.pondId];
    if (!pond) return newState;
    for (const sourcePondId of pond.sources) {
      const leaves = getLeaves(sourcePondId, newState.ripples);
      for (const leaf of leaves) {
        newState = propagateDemandToRipple(leaf.id, rippleId, false, isPersistent, newState);
      }
    }
  }

  return newState;
}

function maybeEagerStop(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  if (!rs) return state;
  if (!isStopped(rs.demand)) return state; // still has non-stop records

  return propagateStopUpstream(rippleId, state);
}

function propagateStopUpstream(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const ripple = state.ripples[rippleId];
  if (!ripple) return state;

  let newState = state;

  if (!isRoot(ripple, state.ripples)) {
    for (const pid of ripple.parents) {
      const parent = state.ripples[pid];
      if (!parent || parent.pondId !== ripple.pondId) continue;
      newState = propagateDemandToRipple(pid, rippleId, true, false, newState);
    }
  } else {
    const pond = state.ponds[ripple.pondId];
    if (!pond) return newState;
    for (const sourcePondId of pond.sources) {
      const leaves = getLeaves(sourcePondId, state.ripples);
      for (const leaf of leaves) {
        newState = propagateDemandToRipple(leaf.id, rippleId, true, false, newState);
      }
    }
  }

  return newState;
}

export function startRipple(
  rippleId: RippleId,
  now: number,
  pulseMode: boolean,
  state: OrchestrState
): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple) return state;

  let newState: OrchestrState = {
    ...state,
    rippleStates: {
      ...state.rippleStates,
      [rippleId]: { ...rs, isRunning: true, runStartedAt: now },
    },
  };

  if (pulseMode) return newState;

  // Demand propagation: wave if any active demand is persistent
  const isPersistent = rs.demand.some((d) => !d.isStop && d.isPersistent);

  if (!isRoot(ripple, state.ripples)) {
    for (const pid of ripple.parents) {
      const parent = state.ripples[pid];
      if (!parent || parent.pondId !== ripple.pondId) continue;
      newState = propagateDemandToRipple(pid, rippleId, false, isPersistent, newState);
    }
  } else {
    const pond = state.ponds[ripple.pondId];
    if (!pond) return newState;
    for (const sourcePondId of pond.sources) {
      const leaves = getLeaves(sourcePondId, state.ripples);
      for (const leaf of leaves) {
        newState = propagateDemandToRipple(leaf.id, rippleId, false, isPersistent, newState);
      }
    }
  }

  return newState;
}

export function completeRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple) return state;

  // 1. Clear all demand records
  // 2. Increment generation
  // 3. Advance watermarks
  const newGeneration = rs.generation + 1;

  // Advance watermarks for intra-pond parents
  const newWatermarks = { ...state.watermarks };
  for (const pid of ripple.parents) {
    const parent = state.ripples[pid];
    if (!parent || parent.pondId !== ripple.pondId) continue;
    const parentState = state.rippleStates[pid];
    if (parentState) {
      newWatermarks[wmKey(pid, rippleId)] = parentState.generation;
    }
  }

  // Advance watermarks for inter-pond leaf ripples (if root)
  if (isRoot(ripple, state.ripples)) {
    const pond = state.ponds[ripple.pondId];
    if (pond) {
      for (const sourcePondId of pond.sources) {
        const leaves = getLeaves(sourcePondId, state.ripples);
        for (const leaf of leaves) {
          const leafState = state.rippleStates[leaf.id];
          if (leafState) {
            newWatermarks[wmKey(leaf.id, rippleId)] = leafState.generation;
          }
        }
      }
    }
  }

  let newState: OrchestrState = {
    ...state,
    watermarks: newWatermarks,
    rippleStates: {
      ...state.rippleStates,
      [rippleId]: {
        generation: newGeneration,
        isRunning: false,
        runStartedAt: null,
        demand: [],
      },
    },
  };

  // Re-arm wave trigger if this ripple is a leaf of a wave-triggered pond
  const trigger = state.triggers[ripple.pondId];
  if (trigger && trigger.kind === 'wave') {
    const leaves = getLeaves(ripple.pondId, state.ripples);
    if (leaves.some((l) => l.id === rippleId)) {
      const leafRs = newState.rippleStates[rippleId];
      newState = {
        ...newState,
        rippleStates: {
          ...newState.rippleStates,
          [rippleId]: {
            ...leafRs,
            demand: upsertDemand(leafRs.demand, 'wave-trigger', false, true),
          },
        },
      };
    }
  }

  return newState;
}

export function tick(now: number, state: OrchestrState): OrchestrState {
  let newState = state;

  // 1. Complete finished runs
  for (const [id, rs] of Object.entries(newState.rippleStates)) {
    if (rs.isRunning && rs.runStartedAt !== null) {
      const ripple = newState.ripples[id];
      if (ripple && now - rs.runStartedAt >= ripple.durationMs) {
        newState = completeRipple(id, newState);
      }
    }
  }

  // 2. Eagerly push demand upstream for Active-but-blocked ripples so cold sources start.
  //    Without this, a wave on an outlet never reaches upstream ponds that are at gen=0.
  for (const id of Object.keys(newState.rippleStates)) {
    const rs = newState.rippleStates[id];
    const ripple = newState.ripples[id];
    if (!rs || !ripple || rs.isRunning) continue;
    if (!isActive(rs.demand)) continue;
    if (sourcesReady(id, newState)) continue;
    newState = propagateActiveUpstream(id, newState);
  }

  // 3. Start eligible ripples
  for (const id of Object.keys(newState.rippleStates)) {
    const { yes, pulseMode } = canRippleStart(id, newState);
    if (yes) {
      newState = startRipple(id, now, pulseMode, newState);
    }
  }

  return newState;
}

export function addDemandToLeaves(
  pondId: PondId,
  sinkId: SinkId,
  isStop: boolean,
  isPersistent: boolean,
  state: OrchestrState
): OrchestrState {
  const leaves = getLeaves(pondId, state.ripples);
  let newState = state;
  for (const leaf of leaves) {
    newState = propagateDemandToRipple(leaf.id, sinkId, isStop, isPersistent, newState);
  }
  return newState;
}

export function addDemandToAllRipples(
  pondId: PondId,
  sinkId: SinkId,
  isStop: boolean,
  isPersistent: boolean,
  state: OrchestrState
): OrchestrState {
  const pondRipples = Object.values(state.ripples).filter((r) => r.pondId === pondId);
  let newState = state;
  for (const r of pondRipples) {
    newState = propagateDemandToRipple(r.id, sinkId, isStop, isPersistent, newState);
  }
  return newState;
}
