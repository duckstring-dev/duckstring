import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  EdgeKindMap,
  EdgeDemandKind,
  ActiveTrigger,
} from './types';
import { getLeaves, getRoots } from './graph';

// ─── Freshness orchestrator ───────────────────────────────────────────────────
//
// One currency: time. Every ripple output carries a freshness timestamp `F`. Demand is
// either a *pull* (resupply: take fresher input + re-arm parents — Tap/Wave) or a *push*
// (priority target `T`: reach freshness ≥ T — Pulse/Tide). Execution is purely at the ripple
// level; ponds are boundaries. The ripple graph is flattened across pond boundaries: a pond
// root's parents are the leaf ripples of its source ponds. Nobody reads a child — demand
// flows up (child signals parent), supply flows down (child reads parent's F).

export interface OrchestrState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  edgeKinds: EdgeKindMap;
  triggers: Record<PondId, ActiveTrigger>;
}

// ─── Logging ────────────────────────────────────────────────────────────────

let LOG_START = 0;
function lt(): string {
  if (LOG_START === 0) LOG_START = Date.now();
  return `t=${((Date.now() - LOG_START) / 1000).toFixed(2)}s`;
}
function rname(state: OrchestrState, id: RippleId): string {
  const r = state.ripples[id];
  return r ? `${state.ponds[r.pondId]?.name ?? '?'}.${r.name}` : id;
}
function log(...args: unknown[]) {
  console.log(lt(), ...args);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const MAX_HISTORY = 500;
function pushHistory(arr: number[], v: number): number[] {
  const next = [...arr, v];
  return next.length > MAX_HISTORY ? next.slice(next.length - MAX_HISTORY) : next;
}

function gaussian(): number {
  let u = 0;
  let v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
function sampleDuration(baseMs: number, variability: number): number {
  if (!variability) return baseMs;
  return baseMs * Math.exp(variability * gaussian());
}

function setRipple(state: OrchestrState, id: RippleId, patch: Partial<RippleRunState>): OrchestrState {
  const rs = state.rippleStates[id];
  if (!rs) return state;
  return { ...state, rippleStates: { ...state.rippleStates, [id]: { ...rs, ...patch } } };
}

function setEdge(state: OrchestrState, key: string, kind: EdgeDemandKind): OrchestrState {
  if (state.edgeKinds[key] === kind) return state;
  return { ...state, edgeKinds: { ...state.edgeKinds, [key]: kind } };
}

// Mark the visual edge between a parent ripple and a child ripple. Intra-pond → ripple edge
// `${parent}::${child}`; inter-pond → pond edge `${sourcePond}::${sinkPond}`.
function markEdge(state: OrchestrState, parentId: RippleId, childId: RippleId, kind: EdgeDemandKind): OrchestrState {
  const p = state.ripples[parentId];
  const c = state.ripples[childId];
  if (!p || !c) return state;
  const key = p.pondId === c.pondId ? `${parentId}::${childId}` : `${p.pondId}::${c.pondId}`;
  return setEdge(state, key, kind);
}

// Flattened parents of a ripple, split required/optional. Intra-pond parents if any; otherwise
// (a pond root) the leaf ripples of the pond's source ponds.
function parentsOf(ripple: Ripple, state: OrchestrState): { required: RippleId[]; optional: RippleId[] } {
  const intra = ripple.parents.filter((pid) => state.ripples[pid]?.pondId === ripple.pondId);
  if (intra.length > 0) {
    const opt = new Set(ripple.optionalParents ?? []);
    return { required: intra.filter((id) => !opt.has(id)), optional: intra.filter((id) => opt.has(id)) };
  }
  const pond = state.ponds[ripple.pondId];
  if (!pond) return { required: [], optional: [] };
  const optSrc = new Set(pond.optionalSources ?? []);
  const required: RippleId[] = [];
  const optional: RippleId[] = [];
  for (const sp of pond.sources) {
    const leaves = getLeaves(sp, state.ripples).map((l) => l.id);
    (optSrc.has(sp) ? optional : required).push(...leaves);
  }
  return { required, optional };
}

// The freshness a run of this ripple would carry: min over required parents, else max over
// optional parents, else `now` (an inlet mints current freshness).
function parentsFreshness(ripple: Ripple, state: OrchestrState, now: number): number {
  const { required, optional } = parentsOf(ripple, state);
  if (required.length > 0) return Math.min(...required.map((id) => state.rippleStates[id]?.F ?? 0));
  if (optional.length > 0) return Math.max(...optional.map((id) => state.rippleStates[id]?.F ?? 0));
  return now;
}

// ─── Demand: pull (Tap/Wave) ────────────────────────────────────────────────

// Cold-start KICK: arm this ripple and recurse to idle parents, so an idle chain wakes all the
// way to its inlets. Used only when demand first arrives (trigger / Tap).
function kickPull(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const r = state.ripples[rippleId];
  if (!rs || !r) return state;
  let ns = rs.hasPull ? state : setRipple(state, rippleId, { hasPull: true });
  const { required, optional } = parentsOf(r, ns);
  for (const pid of [...required, ...optional]) {
    ns = markEdge(ns, pid, rippleId, 'pull');
    const prs = ns.rippleStates[pid];
    if (prs && !prs.isRunning && !prs.hasPull) ns = kickPull(pid, ns);
  }
  return ns;
}

// Shallow ARM: set hasPull on one ripple, no recursion. This is the back-pressure path —
// a node is re-armed only by its direct child running (and the outlet by its Wave each tick),
// so upstream is paced by downstream consumption, not re-kicked every cycle.
function armPull(rippleId: RippleId, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  if (!rs || rs.hasPull) return state;
  return setRipple(state, rippleId, { hasPull: true });
}

// ─── Demand: push (Pulse/Tide) ──────────────────────────────────────────────

function receivePush(rippleId: RippleId, T: number, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const r = state.ripples[rippleId];
  if (!rs || !r) return state;
  let ns = (rs.hasPush ?? 0) >= T ? state : setRipple(state, rippleId, { hasPush: T });
  // Eagerly propagate the target up to parents that aren't fresh enough yet.
  const { required, optional } = parentsOf(r, ns);
  const targets = required.length > 0 ? required : optional;
  for (const pid of targets) {
    const prs = ns.rippleStates[pid];
    if (prs && prs.F < T) {
      ns = markEdge(ns, pid, rippleId, 'push');
      ns = receivePush(pid, T, ns);
    }
  }
  return ns;
}

// ─── Stop ────────────────────────────────────────────────────────────────────

// Clear all demand from the given ripples up through the whole ancestry (BFS, cycle-safe).
// In-flight runs are left to drain.
function stopFrom(startIds: RippleId[], state: OrchestrState): OrchestrState {
  let ns = state;
  const visited = new Set<RippleId>();
  const queue = [...startIds];
  while (queue.length) {
    const id = queue.shift()!;
    if (visited.has(id)) continue;
    visited.add(id);
    const rs = ns.rippleStates[id];
    if (rs && (rs.hasPull || rs.hasPush !== null)) ns = setRipple(ns, id, { hasPull: false, hasPush: null });
    const r = ns.ripples[id];
    if (!r) continue;
    const { required, optional } = parentsOf(r, ns);
    for (const pid of [...required, ...optional]) {
      ns = markEdge(ns, pid, id, 'stop');
      if (!visited.has(pid)) queue.push(pid);
    }
  }
  return ns;
}

// ─── Pond-level trigger entry points (target a pond's leaf ripples) ──────────

// Tap / Wave cold-start: kick the leaves (recurses up idle chains).
export function pullPond(pondId: PondId, state: OrchestrState): OrchestrState {
  let ns = state;
  for (const leaf of getLeaves(pondId, state.ripples)) ns = kickPull(leaf.id, ns);
  return ns;
}

export function pushPond(pondId: PondId, T: number, state: OrchestrState): OrchestrState {
  log(`push ${state.ponds[pondId]?.name ?? pondId} T=${T}`);
  let ns = state;
  for (const leaf of getLeaves(pondId, state.ripples)) ns = receivePush(leaf.id, T, ns);
  return ns;
}

export function stopPond(pondId: PondId, state: OrchestrState): OrchestrState {
  log(`stop ${state.ponds[pondId]?.name ?? pondId}`);
  return stopFrom(getLeaves(pondId, state.ripples).map((l) => l.id), state);
}

// ─── Lifecycle ────────────────────────────────────────────────────────────────

function canRun(rippleId: RippleId, state: OrchestrState, now: number): boolean {
  const rs = state.rippleStates[rippleId];
  const r = state.ripples[rippleId];
  if (!rs || !r || rs.isRunning) return false;
  const pf = parentsFreshness(r, state, now);
  const pullReady = rs.hasPull && pf > rs.F;
  const pushReady = rs.hasPush !== null && pf >= rs.hasPush;
  return pullReady || pushReady;
}

function startRipple(rippleId: RippleId, now: number, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const r = state.ripples[rippleId];
  if (!rs || !r) return state;
  const pf = parentsFreshness(r, state, now);
  const dur = sampleDuration(r.durationMs, r.variability);
  const wasPull = rs.hasPull;
  log(`start ${rname(state, rippleId)} F=${rs.F} → runF=${pf} dur=${(dur / 1000).toFixed(2)}s${wasPull ? ' (pull)' : ''}${rs.hasPush !== null ? ` (push ${rs.hasPush})` : ''}`);

  let ns = setRipple(state, rippleId, {
    isRunning: true,
    runStartedAt: now,
    currentRunDurationMs: dur,
    runFreshness: pf,
    hasPull: false,
    runsStarted: rs.runsStarted + 1,
  });

  // Resupply order: re-arm DIRECT parents only (shallow) so each layer is paced by its own
  // child running — this is the back-pressure. No recursion (that's cold-start's job).
  if (wasPull) {
    const { required, optional } = parentsOf(r, ns);
    for (const pid of [...required, ...optional]) {
      ns = markEdge(ns, pid, rippleId, 'pull');
      ns = armPull(pid, ns);
    }
  }
  return ns;
}

function completeRipple(rippleId: RippleId, now: number, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  if (!rs) return state;
  const F = rs.runFreshness ?? rs.F;
  const pushSatisfied = rs.hasPush !== null && F >= rs.hasPush;
  log(`done ${rname(state, rippleId)} F=${F}${pushSatisfied ? ' (push satisfied)' : ''}`);
  return setRipple(state, rippleId, {
    F,
    isRunning: false,
    runStartedAt: null,
    runFreshness: null,
    hasPush: pushSatisfied ? null : rs.hasPush,
    lastDurationMs: rs.currentRunDurationMs ?? rs.lastDurationMs,
    currentRunDurationMs: null,
    completionTimes: pushHistory(rs.completionTimes, now),
    durations: pushHistory(rs.durations, rs.currentRunDurationMs ?? 0),
    runsCompleted: rs.runsCompleted + 1,
  });
}

// ─── Derived pond rollups (display only) ─────────────────────────────────────

function recomputePonds(state: OrchestrState, now: number): OrchestrState {
  const pondStates = { ...state.pondStates };
  for (const pond of Object.values(state.ponds)) {
    const roots = getRoots(pond.id, state.ripples);
    const leaves = getLeaves(pond.id, state.ripples);
    const prev = state.pondStates[pond.id];
    if (!prev) continue;
    const runsStarted = roots.length ? Math.max(...roots.map((r) => state.rippleStates[r.id]?.runsStarted ?? 0)) : 0;
    const runsCompleted = leaves.length ? Math.min(...leaves.map((l) => state.rippleStates[l.id]?.runsCompleted ?? 0)) : 0;
    const F = leaves.length ? Math.min(...leaves.map((l) => state.rippleStates[l.id]?.F ?? 0)) : 0;
    const hasPull = leaves.some((l) => state.rippleStates[l.id]?.hasPull);
    const pushes = leaves.map((l) => state.rippleStates[l.id]?.hasPush ?? 0).filter((v) => v > 0);
    const hasPush = pushes.length ? Math.max(...pushes) : null;

    let genStart = prev.genStart;
    let completionTimes = prev.completionTimes;
    let durations = prev.durations;
    if (runsStarted > prev.runsStarted) genStart = now;
    if (runsCompleted > prev.runsCompleted) {
      completionTimes = pushHistory(prev.completionTimes, now);
      if (genStart != null) durations = pushHistory(prev.durations, now - genStart);
    }
    pondStates[pond.id] = { F, hasPull, hasPush, runsStarted, runsCompleted, genStart, completionTimes, durations };
  }
  return { ...state, pondStates };
}

// ─── Tick ──────────────────────────────────────────────────────────────────────

export function tick(now: number, state: OrchestrState): OrchestrState {
  let ns = state;

  // 1. complete finished runs
  for (const [id, rs] of Object.entries(ns.rippleStates)) {
    if (rs.isRunning && rs.runStartedAt !== null) {
      const dur = rs.currentRunDurationMs ?? ns.ripples[id]?.durationMs ?? 0;
      if (now - rs.runStartedAt >= dur) ns = completeRipple(id, now, ns);
    }
  }

  // 2. Wave continuously re-arms its outlet leaves (shallow — the outlet is the leaf's standing
  //    consumer). Upstream is paced by re-arm-on-run, not by re-kicking here each tick.
  for (const [pondId, trig] of Object.entries(ns.triggers)) {
    if (trig.kind === 'wave') {
      for (const leaf of getLeaves(pondId, ns.ripples)) ns = armPull(leaf.id, ns);
    }
  }

  // 3. start everything runnable (push targets were propagated eagerly at receive time)
  for (const id of Object.keys(ns.rippleStates)) {
    if (canRun(id, ns, now)) ns = startRipple(id, now, ns);
  }

  // 4. refresh derived pond rollups
  ns = recomputePonds(ns, now);
  return ns;
}
