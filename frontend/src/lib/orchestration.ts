import type {
  PondId,
  RippleId,
  SinkId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  DemandRecord,
  WatermarkMap,
  ActiveTrigger,
} from './types';
import { getLeaves } from './graph';

// Design notes:
// - isStopped is at the Pond level. All ponds start stopped.
// - Non-stop demand wakes a stopped pond immediately and propagates to stopped sources.
// - isStopped transitions back to true only after a run completes when only stop demand remains.
// - Watermarks are pond-to-pond: key `${sourcePondId}::${sinkPondId}`.
// - Ripples have no demand records; they track generationStarted/Completed.
// - Root ripples start when pond.generationStarted > ripple.generationStarted.
// - Non-root ripples start when all intra-pond parents have generationCompleted > ripple.generationStarted.

export interface OrchestrState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  watermarks: WatermarkMap;
  triggers: Record<PondId, ActiveTrigger>;
}

function wmKey(sourceId: string, sinkId: string): string {
  return `${sourceId}::${sinkId}`;
}

function upsertDemand(
  demand: DemandRecord[],
  sinkId: SinkId,
  isStop: boolean,
  isPersistent: boolean
): DemandRecord[] {
  const idx = demand.findIndex((d) => d.sinkId === sinkId);
  const record: DemandRecord = { sinkId, isStop, isPersistent };
  if (idx === -1) return [...demand, record];
  const next = [...demand];
  next[idx] = record;
  return next;
}

function isRoot(ripple: Ripple, ripples: Record<RippleId, Ripple>): boolean {
  return !ripple.parents.some((pid) => ripples[pid]?.pondId === ripple.pondId);
}

// Receive demand at a pond. Handles waking stopped ponds, propagation to stopped sources,
// and stop propagation to all sources.
export function receivePondDemand(
  pondId: PondId,
  sinkId: SinkId,
  isStop: boolean,
  isPersistent: boolean,
  state: OrchestrState
): OrchestrState {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond) return state;

  const newDemand = upsertDemand(ps.demand, sinkId, isStop, isPersistent);

  if (isStop) {
    // Stop demand: mark pond stopped, propagate stop to all sources immediately.
    const newPs: PondRunState = { ...ps, demand: newDemand, isStopped: true };
    let newState: OrchestrState = {
      ...state,
      pondStates: { ...state.pondStates, [pondId]: newPs },
    };
    for (const sourcePondId of pond.sources) {
      newState = receivePondDemand(sourcePondId, pondId, true, false, newState);
    }
    return newState;
  }

  // Non-stop demand
  const newIsWave = ps.isWave || isPersistent;

  let newState: OrchestrState;

  if (ps.isStopped) {
    // Wake from stopped: set hasDemand on ALL ripples (cold-start).
    const newRippleStates = { ...state.rippleStates };
    for (const r of Object.values(state.ripples)) {
      if (r.pondId !== pondId) continue;
      newRippleStates[r.id] = { ...newRippleStates[r.id], hasDemand: true, isWave: newIsWave };
    }
    const newPs: PondRunState = {
      ...ps,
      demand: newDemand,
      isStopped: false,
      hasDemand: true,
      isWave: newIsWave,
    };
    newState = {
      ...state,
      pondStates: { ...state.pondStates, [pondId]: newPs },
      rippleStates: newRippleStates,
    };
  } else {
    // Already active: set hasDemand on leaf ripples only.
    const newRippleStates = { ...state.rippleStates };
    for (const leaf of getLeaves(pondId, state.ripples)) {
      newRippleStates[leaf.id] = { ...newRippleStates[leaf.id], hasDemand: true, isWave: newIsWave };
    }
    const newPs: PondRunState = {
      ...ps,
      demand: newDemand,
      hasDemand: true,
      isWave: newIsWave,
    };
    newState = {
      ...state,
      pondStates: { ...state.pondStates, [pondId]: newPs },
      rippleStates: newRippleStates,
    };
  }

  // Propagate to any stopped sources.
  for (const sourcePondId of pond.sources) {
    if (newState.pondStates[sourcePondId]?.isStopped) {
      newState = receivePondDemand(sourcePondId, pondId, false, isPersistent, newState);
    }
  }

  return newState;
}

function pondSourcesReady(pondId: PondId, state: OrchestrState): boolean {
  const pond = state.ponds[pondId];
  if (!pond || pond.sources.length === 0) return true;
  for (const sourcePondId of pond.sources) {
    const sourcePs = state.pondStates[sourcePondId];
    if (!sourcePs) return false;
    const wm = state.watermarks[wmKey(sourcePondId, pondId)] ?? 0;
    if (sourcePs.generationCompleted <= wm) return false;
  }
  return true;
}

// Attempt to start a new pond generation. Called each tick.
function tryStartPond(pondId: PondId, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond) return state;
  if (ps.isStopped || !ps.hasDemand) return state;
  if (!pondSourcesReady(pondId, state)) return state;

  // Advance watermarks to the source generationCompleted values consumed by this generation.
  const newWatermarks = { ...state.watermarks };
  for (const sourcePondId of pond.sources) {
    const sourcePs = state.pondStates[sourcePondId];
    if (sourcePs) newWatermarks[wmKey(sourcePondId, pondId)] = sourcePs.generationCompleted;
  }

  const wasWave = ps.isWave;
  const newGenerationStarted = ps.generationStarted + 1;

  let newState: OrchestrState = {
    ...state,
    watermarks: newWatermarks,
    pondStates: {
      ...state.pondStates,
      [pondId]: {
        ...ps,
        generationStarted: newGenerationStarted,
        hasDemand: false,
        isWave: false,
      },
    },
  };

  // Propagate wave to sources on start.
  if (wasWave) {
    for (const sourcePondId of pond.sources) {
      newState = receivePondDemand(sourcePondId, pondId, false, true, newState);
    }
  }

  return newState;
}

function canRippleStart(rippleId: RippleId, state: OrchestrState): boolean {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple || rs.isRunning) return false;

  const ps = state.pondStates[ripple.pondId];
  if (!ps || ps.isStopped) return false;

  if (isRoot(ripple, state.ripples)) {
    return ps.generationStarted > rs.generationStarted;
  }

  // Non-root: all intra-pond parents must have completed a generation newer than this ripple's started.
  if (ps.generationStarted === 0) return false;
  for (const pid of ripple.parents) {
    const parent = state.ripples[pid];
    if (!parent || parent.pondId !== ripple.pondId) continue;
    const parentRs = state.rippleStates[pid];
    if (!parentRs || parentRs.generationCompleted <= rs.generationStarted) return false;
  }
  return true;
}

function startRipple(rippleId: RippleId, now: number, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple) return state;

  const ps = state.pondStates[ripple.pondId];
  if (!ps) return state;

  let newGenStarted: number;
  if (isRoot(ripple, state.ripples)) {
    newGenStarted = ps.generationStarted;
  } else {
    const parentCompletions = ripple.parents
      .filter((pid) => state.ripples[pid]?.pondId === ripple.pondId)
      .map((pid) => state.rippleStates[pid]?.generationCompleted ?? 0);
    newGenStarted = parentCompletions.length > 0 ? Math.min(...parentCompletions) : ps.generationStarted;
  }

  const wasWave = rs.isWave;

  let newState: OrchestrState = {
    ...state,
    rippleStates: {
      ...state.rippleStates,
      [rippleId]: {
        ...rs,
        generationStarted: newGenStarted,
        isRunning: true,
        runStartedAt: now,
        hasDemand: false,
        isWave: false,
      },
    },
  };

  // Wave propagation to intra-pond parents on start.
  if (wasWave && !ps.isStopped && !isRoot(ripple, state.ripples)) {
    for (const pid of ripple.parents) {
      const parent = state.ripples[pid];
      if (!parent || parent.pondId !== ripple.pondId) continue;
      const parentRs = newState.rippleStates[pid];
      if (parentRs) {
        newState = {
          ...newState,
          rippleStates: {
            ...newState.rippleStates,
            [pid]: { ...parentRs, hasDemand: true, isWave: true },
          },
        };
      }
    }
  }

  return newState;
}

function completeRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const ripple = state.ripples[rippleId];
  if (!rs || !ripple) return state;

  let newState: OrchestrState = {
    ...state,
    rippleStates: {
      ...state.rippleStates,
      [rippleId]: {
        ...rs,
        generationCompleted: rs.generationStarted,
        isRunning: false,
        runStartedAt: null,
      },
    },
  };

  return updatePondGenerationCompleted(ripple.pondId, newState);
}

function updatePondGenerationCompleted(pondId: PondId, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  if (!ps) return state;

  const pondRipples = Object.values(state.ripples).filter((r) => r.pondId === pondId);
  if (pondRipples.length === 0) return state;

  const minCompleted = Math.min(
    ...pondRipples.map((r) => state.rippleStates[r.id]?.generationCompleted ?? 0)
  );

  if (minCompleted <= ps.generationCompleted) return state;

  let newPs: PondRunState = { ...ps, generationCompleted: minCompleted };
  let newState: OrchestrState = {
    ...state,
    pondStates: { ...state.pondStates, [pondId]: newPs },
  };

  // After a generation completes: if all demand is stop, transition to stopped.
  const hasActiveDemand = newPs.demand.some((d) => !d.isStop);
  if (!hasActiveDemand && newPs.demand.length > 0) {
    newPs = { ...newPs, isStopped: true, demand: [] };
    newState = { ...newState, pondStates: { ...newState.pondStates, [pondId]: newPs } };
  }

  // Re-arm wave trigger if pond is still active.
  const trigger = state.triggers[pondId];
  if (trigger?.kind === 'wave' && !newPs.isStopped) {
    newState = receivePondDemand(pondId, 'wave-trigger', false, true, newState);
  }

  return newState;
}

export function tick(now: number, state: OrchestrState): OrchestrState {
  let newState = state;

  // 1. Complete finished runs.
  for (const [id, rs] of Object.entries(newState.rippleStates)) {
    if (rs.isRunning && rs.runStartedAt !== null) {
      const ripple = newState.ripples[id];
      if (ripple && now - rs.runStartedAt >= ripple.durationMs) {
        newState = completeRipple(id, newState);
      }
    }
  }

  // 2. Try to start ponds.
  for (const pondId of Object.keys(newState.pondStates)) {
    newState = tryStartPond(pondId, newState);
  }

  // 3. Start eligible ripples.
  for (const id of Object.keys(newState.rippleStates)) {
    if (canRippleStart(id, newState)) {
      newState = startRipple(id, now, newState);
    }
  }

  return newState;
}
