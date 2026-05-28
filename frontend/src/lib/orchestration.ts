import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  WatermarkMap,
  ActiveTrigger,
} from './types';
import { getLeaves, getRoots } from './graph';

// Orchestration model — see also CLAUDE.md "Orchestration".
//
// Conceptually each Pond has two zero-duration boundaries:
//   P.end   — where downstream consumers / triggers enter signals
//   P.start — where signals exit toward source ponds
//
// State stored on P:
//   isWave    — latched only by wave reaching P.start (not P.end), cleared by stop or advancePond
//   hasDemand — set only by pulse, start, or wave reaching P.start; cleared in advancePond
//
// Ripples have no isWave — they read pond.isWave (and propagation happens via advancePond, not at ripple start).
//
// Maintenance: a WaveTrigger lives on an outlet pond and re-fires receiveWave_at_end on each pond gen
// completion. No node ever rearms itself.

export interface OrchestrState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  watermarks: WatermarkMap;
  triggers: Record<PondId, ActiveTrigger>;
}

function wmKey(parentId: string, childId: string): string {
  return `${parentId}::${childId}`;
}

function isRoot(ripple: Ripple, ripples: Record<RippleId, Ripple>): boolean {
  return !ripple.parents.some((pid) => ripples[pid]?.pondId === ripple.pondId);
}

function isLeaf(ripple: Ripple, ripples: Record<RippleId, Ripple>): boolean {
  return !Object.values(ripples).some(
    (r) => r.pondId === ripple.pondId && r.parents.includes(ripple.id)
  );
}

function intraPondParents(ripple: Ripple, ripples: Record<RippleId, Ripple>): Ripple[] {
  return ripple.parents
    .map((pid) => ripples[pid])
    .filter((r): r is Ripple => !!r && r.pondId === ripple.pondId);
}

function intraPondChildren(
  rippleId: RippleId,
  pondId: PondId,
  ripples: Record<RippleId, Ripple>
): Ripple[] {
  return Object.values(ripples).filter((r) => r.pondId === pondId && r.parents.includes(rippleId));
}

function setRipple(
  state: OrchestrState,
  rippleId: RippleId,
  patch: Partial<RippleRunState>
): OrchestrState {
  const rs = state.rippleStates[rippleId];
  if (!rs) return state;
  return {
    ...state,
    rippleStates: { ...state.rippleStates, [rippleId]: { ...rs, ...patch } },
  };
}

function setPond(
  state: OrchestrState,
  pondId: PondId,
  patch: Partial<PondRunState>
): OrchestrState {
  const ps = state.pondStates[pondId];
  if (!ps) return state;
  return {
    ...state,
    pondStates: { ...state.pondStates, [pondId]: { ...ps, ...patch } },
  };
}

// ─── Signal handlers ────────────────────────────────────────────────────────

function receivePulseRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  if (!r) return state;
  let newState = setRipple(state, rippleId, { hasDemand: true });
  if (isRoot(r, state.ripples)) {
    newState = receivePulseAtStart(r.pondId, newState);
  } else {
    for (const parent of intraPondParents(r, state.ripples)) {
      newState = receivePulseRipple(parent.id, newState);
    }
  }
  return newState;
}

function receiveWaveRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  if (!r) return state;
  let newState = setRipple(state, rippleId, { hasDemand: true });
  if (isRoot(r, state.ripples)) {
    newState = receiveWaveAtStart(r.pondId, newState);
  } else {
    for (const parent of intraPondParents(r, state.ripples)) {
      const parentRs = newState.rippleStates[parent.id];
      if (parentRs && !parentRs.isRunning && !parentRs.hasDemand) {
        newState = receiveWaveRipple(parent.id, newState);
      }
    }
  }
  return newState;
}

function receiveStopRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  if (!r) return state;
  let newState = setRipple(state, rippleId, { hasDemand: false });
  if (isRoot(r, state.ripples)) {
    newState = receiveStopAtStart(r.pondId, newState);
  } else {
    for (const parent of intraPondParents(r, state.ripples)) {
      newState = receiveStopRipple(parent.id, newState);
    }
  }
  return newState;
}

export function receivePulseAtEnd(pondId: PondId, state: OrchestrState): OrchestrState {
  let newState = state;
  for (const leaf of getLeaves(pondId, state.ripples)) {
    newState = receivePulseRipple(leaf.id, newState);
  }
  return newState;
}

export function receiveWaveAtEnd(pondId: PondId, state: OrchestrState): OrchestrState {
  let newState = state;
  for (const leaf of getLeaves(pondId, state.ripples)) {
    const rs = newState.rippleStates[leaf.id];
    if (rs && !rs.isRunning && !rs.hasDemand) {
      newState = receiveWaveRipple(leaf.id, newState);
    }
  }
  return newState;
}

export function receiveStopAtEnd(pondId: PondId, state: OrchestrState): OrchestrState {
  let newState = setPond(state, pondId, { isWave: false, hasDemand: false });
  for (const leaf of getLeaves(pondId, state.ripples)) {
    newState = receiveStopRipple(leaf.id, newState);
  }
  return newState;
}

function receivePulseAtStart(pondId: PondId, state: OrchestrState): OrchestrState {
  const pond = state.ponds[pondId];
  if (!pond) return state;
  let newState = setPond(state, pondId, { hasDemand: true });
  for (const sP of pond.sources) {
    newState = receivePulseAtEnd(sP, newState);
  }
  return newState;
}

function receiveWaveAtStart(pondId: PondId, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond) return state;
  const wasWave = ps.isWave;
  let newState = setPond(state, pondId, { isWave: true, hasDemand: true });
  if (!wasWave) {
    for (const sP of pond.sources) {
      newState = receiveWaveAtEnd(sP, newState);
    }
  }
  return newState;
}

function receiveStopAtStart(pondId: PondId, state: OrchestrState): OrchestrState {
  const pond = state.ponds[pondId];
  if (!pond) return state;
  let newState = setPond(state, pondId, { isWave: false, hasDemand: false });
  for (const sP of pond.sources) {
    newState = receiveStopAtEnd(sP, newState);
  }
  return newState;
}

// Direct (no propagation)
export function receiveStart(pondId: PondId, state: OrchestrState): OrchestrState {
  return setPond(state, pondId, { hasDemand: true });
}

// ─── Lifecycle ──────────────────────────────────────────────────────────────

function canStartRipple(rippleId: RippleId, state: OrchestrState): boolean {
  const r = state.ripples[rippleId];
  const rs = state.rippleStates[rippleId];
  if (!r || !rs) return false;
  if (rs.isRunning || !rs.hasDemand) return false;
  if (isRoot(r, state.ripples)) {
    const ps = state.pondStates[r.pondId];
    if (!ps) return false;
    return ps.generationStarted > (state.watermarks[wmKey(r.pondId, rippleId)] ?? 0);
  }
  for (const parent of intraPondParents(r, state.ripples)) {
    const parentRs = state.rippleStates[parent.id];
    if (!parentRs) return false;
    if (parentRs.generationCompleted <= (state.watermarks[wmKey(parent.id, rippleId)] ?? 0)) {
      return false;
    }
  }
  return true;
}

function startRipple(rippleId: RippleId, now: number, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  const rs = state.rippleStates[rippleId];
  if (!r || !rs) return state;

  const newWatermarks = { ...state.watermarks };
  let newGenStarted: number;
  if (isRoot(r, state.ripples)) {
    const ps = state.pondStates[r.pondId];
    if (!ps) return state;
    newGenStarted = ps.generationStarted;
    newWatermarks[wmKey(r.pondId, rippleId)] = ps.generationStarted;
  } else {
    const parents = intraPondParents(r, state.ripples);
    newGenStarted = Math.min(
      ...parents.map((p) => state.rippleStates[p.id]?.generationCompleted ?? 0)
    );
    for (const p of parents) {
      newWatermarks[wmKey(p.id, rippleId)] = state.rippleStates[p.id]?.generationCompleted ?? 0;
    }
  }

  return {
    ...state,
    watermarks: newWatermarks,
    rippleStates: {
      ...state.rippleStates,
      [rippleId]: {
        ...rs,
        generationStarted: newGenStarted,
        isRunning: true,
        runStartedAt: now,
        hasDemand: false,
      },
    },
  };
}

function completeRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  const rs = state.rippleStates[rippleId];
  if (!r || !rs) return state;

  const newRippleStates = { ...state.rippleStates };
  newRippleStates[rippleId] = {
    ...rs,
    generationCompleted: rs.generationStarted,
    isRunning: false,
    runStartedAt: null,
  };

  // Baton-pass within pond: enable intra-pond children to run this gen
  for (const child of intraPondChildren(rippleId, r.pondId, state.ripples)) {
    const childRs = newRippleStates[child.id];
    if (childRs) {
      newRippleStates[child.id] = { ...childRs, hasDemand: true };
    }
  }

  let newState: OrchestrState = { ...state, rippleStates: newRippleStates };

  if (isLeaf(r, state.ripples)) {
    newState = updatePondCompleted(r.pondId, newState);
  }

  return newState;
}

function updatePondCompleted(pondId: PondId, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  if (!ps) return state;
  const leaves = getLeaves(pondId, state.ripples);
  if (leaves.length === 0) return state;
  const newCompleted = Math.min(
    ...leaves.map((l) => state.rippleStates[l.id]?.generationCompleted ?? 0)
  );
  if (newCompleted <= ps.generationCompleted) return state;

  let newState = setPond(state, pondId, { generationCompleted: newCompleted });

  // Wave trigger lives on outlets — re-fire on each pond gen completion.
  const trigger = state.triggers[pondId];
  if (trigger?.kind === 'wave') {
    newState = receiveWaveAtEnd(pondId, newState);
  }

  return newState;
}

function canAdvancePond(pondId: PondId, state: OrchestrState): boolean {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond || !ps.hasDemand) return false;
  for (const sP of pond.sources) {
    const sourcePs = state.pondStates[sP];
    if (!sourcePs) return false;
    if (sourcePs.generationCompleted <= (state.watermarks[wmKey(sP, pondId)] ?? 0)) return false;
  }
  return true;
}

function advancePond(pondId: PondId, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond) return state;

  const wasWave = ps.isWave;
  const newWatermarks = { ...state.watermarks };
  for (const sP of pond.sources) {
    const sourcePs = state.pondStates[sP];
    if (sourcePs) newWatermarks[wmKey(sP, pondId)] = sourcePs.generationCompleted;
  }

  const newRippleStates = { ...state.rippleStates };
  for (const root of getRoots(pondId, state.ripples)) {
    const rootRs = newRippleStates[root.id];
    if (rootRs) {
      newRippleStates[root.id] = { ...rootRs, hasDemand: true };
    }
  }

  let newState: OrchestrState = {
    ...state,
    watermarks: newWatermarks,
    rippleStates: newRippleStates,
    pondStates: {
      ...state.pondStates,
      [pondId]: {
        ...ps,
        generationStarted: ps.generationStarted + 1,
        hasDemand: false,
        isWave: false,
      },
    },
  };

  if (wasWave) {
    for (const sP of pond.sources) {
      newState = receiveWaveAtEnd(sP, newState);
    }
  }

  return newState;
}

export function tick(now: number, state: OrchestrState): OrchestrState {
  let newState = state;

  for (const [id, rs] of Object.entries(newState.rippleStates)) {
    if (rs.isRunning && rs.runStartedAt !== null) {
      const r = newState.ripples[id];
      if (r && now - rs.runStartedAt >= r.durationMs) {
        newState = completeRipple(id, newState);
      }
    }
  }

  for (const pondId of Object.keys(newState.pondStates)) {
    if (canAdvancePond(pondId, newState)) {
      newState = advancePond(pondId, newState);
    }
  }

  for (const id of Object.keys(newState.rippleStates)) {
    if (canStartRipple(id, newState)) {
      newState = startRipple(id, now, newState);
    }
  }

  return newState;
}
