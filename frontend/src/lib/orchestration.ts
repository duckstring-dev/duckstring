import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  WatermarkMap,
  EdgeKindMap,
  EdgeDemandKind,
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
//   isWave    — *pending* wave intent: set by a wave reaching P.start, cleared by advancePond.
//   hasDemand — set only by pulse, start, or wave reaching P.start; cleared in advancePond.
//
// Wave-ness of an actual run lives on the PondGeneration record (pond.generations[n].isWave),
// captured from pond.isWave when advancePond arms the generation. Ripples have no wave flag —
// a ripple reads its generation's isWave to decide whether to execute as wave. Stop flips
// in-flight generations' isWave to false so re-saturation halts while the run drains.
//
// Maintenance: a WaveTrigger lives on an outlet pond and re-fires receiveWave_at_end on each pond gen
// completion. No node ever rearms itself.

export interface OrchestrState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  watermarks: WatermarkMap;
  edgeKinds: EdgeKindMap;
  triggers: Record<PondId, ActiveTrigger>;
}

function wmKey(parentId: string, childId: string): string {
  return `${parentId}::${childId}`;
}

// Record the kind of demand most recently sent across an edge (same keying as watermarks).
function setEdge(state: OrchestrState, key: string, kind: EdgeDemandKind): OrchestrState {
  if (state.edgeKinds[key] === kind) return state;
  return { ...state, edgeKinds: { ...state.edgeKinds, [key]: kind } };
}

// Standard normal via Box–Muller.
function gaussian(): number {
  let u = 0;
  let v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

// Sample a run duration: log-normal around the base, with variability as the log-scale SD.
function sampleDuration(baseMs: number, variability: number): number {
  if (!variability) return baseMs;
  return baseMs * Math.exp(variability * gaussian());
}

const MAX_COMPLETION_HISTORY = 500;
function pushCompletion(times: number[], now: number): number[] {
  const next = [...times, now];
  return next.length > MAX_COMPLETION_HISTORY ? next.slice(next.length - MAX_COMPLETION_HISTORY) : next;
}

// ─── Logging ────────────────────────────────────────────────────────────────

let LOG_START = 0;
function lt(): string {
  if (LOG_START === 0) LOG_START = Date.now();
  const ms = Date.now() - LOG_START;
  return `t=${(ms / 1000).toFixed(2)}s`;
}
function pname(state: OrchestrState, id: PondId): string {
  return state.ponds[id]?.name ?? id;
}
function rname(state: OrchestrState, id: RippleId): string {
  const r = state.ripples[id];
  return r ? `${state.ponds[r.pondId]?.name ?? '?'}.${r.name}` : id;
}
function log(...args: unknown[]) {
  console.log(lt(), ...args);
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

// Stop a pond: clear pending wave/demand and flip every in-flight generation's
// isWave to false so ripples stop re-saturating. The hasDemand baton still drains
// each started run to completion; only cancel halts a run mid-flight.
function stopPond(state: OrchestrState, pondId: PondId): OrchestrState {
  const ps = state.pondStates[pondId];
  if (!ps) return state;
  const generations = { ...ps.generations };
  for (let g = ps.generationCompleted + 1; g <= ps.generationStarted; g++) {
    if (generations[g]) generations[g] = { ...generations[g], isWave: false };
  }
  return setPond(state, pondId, { isWave: false, hasRootDemand: false, hasLeafDemand: false, generations });
}

// ─── Signal handlers ────────────────────────────────────────────────────────

function receivePulseRipple(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  if (!r) return state;
  let newState = setRipple(state, rippleId, { hasDemand: true });
  for (const parent of intraPondParents(r, state.ripples)) {
    newState = setEdge(newState, wmKey(parent.id, rippleId), 'pulse');
  }
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
  for (const parent of intraPondParents(r, state.ripples)) {
    newState = setEdge(newState, wmKey(parent.id, rippleId), 'wave');
  }
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
  for (const parent of intraPondParents(r, state.ripples)) {
    newState = setEdge(newState, wmKey(parent.id, rippleId), 'stop');
  }
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
  log(`recvPulse@end ${pname(state, pondId)} → hasLeafDemand=true`);
  let newState = setPond(state, pondId, { hasLeafDemand: true });
  for (const leaf of getLeaves(pondId, state.ripples)) {
    newState = receivePulseRipple(leaf.id, newState);
  }
  return newState;
}

export function receiveWaveAtEnd(pondId: PondId, state: OrchestrState, quiet = false): OrchestrState {
  if (!quiet) log(`recvWave@end ${pname(state, pondId)} → hasLeafDemand=true`);
  let newState = setPond(state, pondId, { hasLeafDemand: true });
  for (const leaf of getLeaves(pondId, state.ripples)) {
    const rs = newState.rippleStates[leaf.id];
    if (rs && !rs.isRunning && !rs.hasDemand) {
      newState = receiveWaveRipple(leaf.id, newState);
    }
  }
  return newState;
}

// Consumer-side back-pressure. A pond asks a source for another generation only once it has
// consumed everything that source has produced — i.e. its watermark has caught up to the
// source's completed generation. While it still holds an unconsumed generation from a source it
// stays quiet, and that silence is what paces the source to this consumer's rate. Demand only
// ever flows upward, held by the consumer; nothing here reads a sink's state.
function demandSources(pondId: PondId, state: OrchestrState): OrchestrState {
  const pond = state.ponds[pondId];
  if (!pond) return state;
  let newState = state;
  for (const sP of pond.sources) {
    const sps = newState.pondStates[sP];
    if (!sps) continue;
    if ((newState.watermarks[wmKey(sP, pondId)] ?? 0) >= sps.generationCompleted) {
      newState = setEdge(newState, wmKey(sP, pondId), 'wave');
      newState = receiveWaveAtEnd(sP, newState);
    }
  }
  return newState;
}

export function receiveStopAtEnd(pondId: PondId, state: OrchestrState): OrchestrState {
  log(`recvStop@end ${pname(state, pondId)}`);
  let newState = stopPond(state, pondId);
  for (const leaf of getLeaves(pondId, state.ripples)) {
    newState = receiveStopRipple(leaf.id, newState);
  }
  return newState;
}

function receivePulseAtStart(pondId: PondId, state: OrchestrState): OrchestrState {
  const pond = state.ponds[pondId];
  if (!pond) return state;
  log(`recvPulse@start ${pname(state, pondId)} → hasRootDemand=true; propagating to sources [${pond.sources.map((s) => pname(state, s)).join(',')}]`);
  let newState = setPond(state, pondId, { hasRootDemand: true });
  for (const sP of pond.sources) {
    newState = setEdge(newState, wmKey(sP, pondId), 'pulse');
    newState = receivePulseAtEnd(sP, newState);
  }
  return newState;
}

function receiveWaveAtStart(pondId: PondId, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond) return state;
  const wasWave = ps.isWave;
  log(`recvWave@start ${pname(state, pondId)} wasWave=${wasWave} → hasRootDemand=true,isWave=true${!wasWave ? `; propagating to sources [${pond.sources.map((s) => pname(state, s)).join(',')}]` : ' (no further propagation, already wave)'}`);
  let newState = setPond(state, pondId, { isWave: true, hasRootDemand: true });
  if (!wasWave) {
    newState = demandSources(pondId, newState);
  }
  return newState;
}

function receiveStopAtStart(pondId: PondId, state: OrchestrState): OrchestrState {
  const pond = state.ponds[pondId];
  if (!pond) return state;
  let newState = stopPond(state, pondId);
  for (const sP of pond.sources) {
    newState = setEdge(newState, wmKey(sP, pondId), 'stop');
    newState = receiveStopAtEnd(sP, newState);
  }
  return newState;
}

// Direct (no propagation). A user "start" is a cold kick: set both demands so the
// pond advances once (cold start couples leaf demand → root demand).
export function receiveStart(pondId: PondId, state: OrchestrState): OrchestrState {
  return setPond(state, pondId, { hasRootDemand: true, hasLeafDemand: true });
}

// ─── Lifecycle ──────────────────────────────────────────────────────────────

function canStartRipple(rippleId: RippleId, state: OrchestrState): boolean {
  const r = state.ripples[rippleId];
  const rs = state.rippleStates[rippleId];
  if (!r || !rs) return false;
  if (rs.isRunning || !rs.hasDemand) return false;
  // Leaf gate: a leaf feeds P.end, which "consumes" a generation only when the whole pond
  // completes it. A leaf must not run ahead of that consumption — it can start its next
  // generation only once the pond has completed (consumed) its previous output. This bounds
  // concurrent in-flight generations to the topological depth of the ripple DAG: a single
  // layer of leaves runs in lockstep (1 in flight), deeper chains pipeline one per layer.
  if (isLeaf(r, state.ripples)) {
    const ps = state.pondStates[r.pondId];
    if (!ps) return false;
    if (rs.generationCompleted > ps.generationCompleted) return false;
  }
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

  const rootFlag = isRoot(r, state.ripples);
  const runIsWave = state.pondStates[r.pondId]?.generations[newGenStarted]?.isWave ?? false;
  const runDurationMs = sampleDuration(r.durationMs, r.variability);
  log(`startRipple ${rname(state, rippleId)} gen=${newGenStarted} ${rootFlag ? '(root)' : '(non-root)'}${isLeaf(r, state.ripples) ? ' (leaf)' : ''} runIsWave=${runIsWave} dur=${(runDurationMs / 1000).toFixed(2)}s`);

  let newState: OrchestrState = {
    ...state,
    watermarks: newWatermarks,
    rippleStates: {
      ...state.rippleStates,
      [rippleId]: {
        ...rs,
        generationStarted: newGenStarted,
        isRunning: true,
        runStartedAt: now,
        currentRunDurationMs: runDurationMs,
        hasDemand: false,
      },
    },
  };

  // On start, if this run is wave, signal upstream so the next generation can pipeline.
  // Root: propagate DIRECTLY to source ponds' ends — do NOT arm this pond's own start.
  // Non-root: register root demand on this pond (and propagate to sources) via
  //   receiveWaveAtStart. We deliberately do NOT re-arm intra-pond parents' hasDemand:
  //   parents are armed by advancePond (roots) and the completion baton (children), and
  //   arming them here strands hasDemand on them when the wave peters out. The pond's
  //   hasRootDemand alone is enough to pipeline — it still requires hasLeafDemand to advance.
  if (runIsWave) {
    if (rootFlag) {
      const pond = state.ponds[r.pondId];
      if (pond) {
        log(`  ↑ propagate wave from ${rname(state, rippleId)} to ready sources [${pond.sources.map((s) => pname(state, s)).join(',')}]`);
        newState = demandSources(r.pondId, newState);
      }
    } else {
      newState = receiveWaveAtStart(r.pondId, newState);
    }
  }

  return newState;
}

function completeRipple(rippleId: RippleId, now: number, state: OrchestrState): OrchestrState {
  const r = state.ripples[rippleId];
  const rs = state.rippleStates[rippleId];
  if (!r || !rs) return state;

  const children = intraPondChildren(rippleId, r.pondId, state.ripples);
  log(`completeRipple ${rname(state, rippleId)} gen=${rs.generationStarted}${children.length > 0 ? ` → baton hasDemand to [${children.map((c) => rname(state, c.id)).join(',')}]` : ''}${isLeaf(r, state.ripples) ? ' (leaf)' : ''}`);

  const newRippleStates = { ...state.rippleStates };
  newRippleStates[rippleId] = {
    ...rs,
    generationCompleted: rs.generationStarted,
    isRunning: false,
    runStartedAt: null,
    lastDurationMs: rs.currentRunDurationMs ?? rs.lastDurationMs,
    currentRunDurationMs: null,
    completionTimes: pushCompletion(rs.completionTimes, now),
    durations: pushCompletion(rs.durations, rs.currentRunDurationMs ?? rs.lastDurationMs ?? 0),
  };

  for (const child of children) {
    const childRs = newRippleStates[child.id];
    if (childRs) {
      newRippleStates[child.id] = { ...childRs, hasDemand: true };
    }
  }

  let newState: OrchestrState = { ...state, rippleStates: newRippleStates };

  if (isLeaf(r, state.ripples)) {
    newState = updatePondCompleted(r.pondId, now, newState);
  }

  return newState;
}

function updatePondCompleted(pondId: PondId, now: number, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  if (!ps) return state;
  const leaves = getLeaves(pondId, state.ripples);
  if (leaves.length === 0) return state;
  const newCompleted = Math.min(
    ...leaves.map((l) => state.rippleStates[l.id]?.generationCompleted ?? 0)
  );
  if (newCompleted <= ps.generationCompleted) return state;

  log(`pondCompleted ${pname(state, pondId)} gen=${newCompleted}`);

  // Generation latency: completion time − the time the generation was armed. Drop armed
  // timestamps for every generation now consumed.
  const genStartTimes = { ...ps.genStartTimes };
  const armed = genStartTimes[newCompleted];
  for (const g of Object.keys(genStartTimes)) {
    if (Number(g) <= newCompleted) delete genStartTimes[Number(g)];
  }
  const durations = armed != null ? pushCompletion(ps.durations, now - armed) : ps.durations;

  // Wave re-supply is handled per-tick in tick() (the trigger acts as a continuous sink),
  // so completion only needs to record the new completed generation and its timestamp here.
  return setPond(state, pondId, {
    generationCompleted: newCompleted,
    completionTimes: pushCompletion(ps.completionTimes, now),
    durations,
    genStartTimes,
  });
}

function canAdvancePond(pondId: PondId, state: OrchestrState): boolean {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond || !ps.hasRootDemand || !ps.hasLeafDemand) return false;
  // Roots must have started the current generation before pond can admit the next one,
  // otherwise pond.generationStarted would race ahead of what ripples can consume.
  for (const root of getRoots(pondId, state.ripples)) {
    const rootRs = state.rippleStates[root.id];
    if (!rootRs) return false;
    if (rootRs.generationStarted < ps.generationStarted) return false;
  }
  for (const sP of pond.sources) {
    const sourcePs = state.pondStates[sP];
    if (!sourcePs) return false;
    if (sourcePs.generationCompleted <= (state.watermarks[wmKey(sP, pondId)] ?? 0)) return false;
  }
  return true;
}

function advancePond(pondId: PondId, now: number, state: OrchestrState): OrchestrState {
  const ps = state.pondStates[pondId];
  const pond = state.ponds[pondId];
  if (!ps || !pond) return state;

  const wasWave = ps.isWave;
  const newGen = ps.generationStarted + 1;
  const roots = getRoots(pondId, state.ripples);
  log(`advancePond ${pname(state, pondId)} gen=${newGen} wasWave=${wasWave} sources=[${pond.sources.map((s) => `${pname(state, s)}.gen=${state.pondStates[s]?.generationCompleted}`).join(',')}] roots=[${roots.map((r) => rname(state, r.id)).join(',')}]`);

  const newWatermarks = { ...state.watermarks };
  for (const sP of pond.sources) {
    const sourcePs = state.pondStates[sP];
    if (sourcePs) newWatermarks[wmKey(sP, pondId)] = sourcePs.generationCompleted;
  }

  const newRippleStates = { ...state.rippleStates };
  for (const root of roots) {
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
        generationStarted: newGen,
        hasRootDemand: false,
        hasLeafDemand: false,
        isWave: false,
        generations: { ...ps.generations, [newGen]: { number: newGen, isWave: wasWave } },
        genStartTimes: { ...ps.genStartTimes, [newGen]: now },
      },
    },
  };

  if (wasWave) {
    newState = demandSources(pondId, newState);
  }

  return newState;
}

export function tick(now: number, state: OrchestrState): OrchestrState {
  let newState = state;

  for (const [id, rs] of Object.entries(newState.rippleStates)) {
    if (rs.isRunning && rs.runStartedAt !== null) {
      const r = newState.ripples[id];
      const dur = rs.currentRunDurationMs ?? r?.durationMs ?? 0;
      if (r && now - rs.runStartedAt >= dur) {
        newState = completeRipple(id, now, newState);
      }
    }
  }

  // A wave trigger is a permanent zero-duration downstream sink: it re-asserts demand at
  // its pond's end every tick. This keeps hasLeafDemand alive between pond completions so
  // a multi-ripple pond can pipeline (advance the next generation the moment a root frees
  // up), and re-arms an idle chain (the single-ripple cold re-arm). Quiet: no per-tick log.
  for (const [pondId, trigger] of Object.entries(newState.triggers)) {
    if (trigger.kind === 'wave') {
      newState = receiveWaveAtEnd(pondId, newState, true);
    }
  }

  for (const pondId of Object.keys(newState.pondStates)) {
    if (canAdvancePond(pondId, newState)) {
      newState = advancePond(pondId, now, newState);
    }
  }

  for (const id of Object.keys(newState.rippleStates)) {
    if (canStartRipple(id, newState)) {
      newState = startRipple(id, now, newState);
    }
  }

  return newState;
}
