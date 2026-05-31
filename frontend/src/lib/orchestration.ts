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

// A pond root has no intra-pond parents (its inputs, if any, are source-pond leaves).
function isPondRoot(ripple: Ripple, state: OrchestrState): boolean {
  return !ripple.parents.some((pid) => state.ripples[pid]?.pondId === ripple.pondId);
}

// Is this ripple's pond the trigger pond, or upstream of one (so a standing trigger's demand
// flows through it)? Used to gate pipelining to standing-demand chains only.
function feedsActiveTrigger(ripple: Ripple, state: OrchestrState): boolean {
  const start = ripple.pondId;
  const seen = new Set<PondId>();
  const stack = [start];
  while (stack.length) {
    const pid = stack.pop()!;
    if (seen.has(pid)) continue;
    seen.add(pid);
    if (state.triggers[pid]) return true;
    // walk to ponds that have this pond as a source (downstream)
    for (const p of Object.values(state.ponds)) {
      if (p.sources.includes(pid)) stack.push(p.id);
    }
  }
  return false;
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

// Arm a ripple with pull, then propagate upward only as far as needed to keep a producer working:
//   - If this node is IDLE and CAN run now (a parent fresher than it), stop — it will run and
//     re-arm its own parents on start. This is the back-pressure: a ready node isn't re-kicked,
//     and upstream is paced by it running, not by repeated demand.
//   - If this node CANNOT run yet (no parent fresher than it), it needs fresher input → recurse
//     into stale parents to wake a producer. Without this, demand dies at a node that can't
//     satisfy it (deadlock).
//   - If this node is RUNNING, it can't consume the pull now, but its NEXT run will need fresh
//     input → propagate up so parents pipeline the next generation while this node is busy.
//     Without this, a slow leaf (the bottleneck) finishes and finds its inputs stale, stalling
//     a beat each cycle instead of running back-to-back.
// Cold start (all F=0) recurses to the inlets because 0 <= 0. Used everywhere pull is asserted:
// trigger/Tap, run-start re-arm, and the per-tick Wave.
function armPull(rippleId: RippleId, now: number, state: OrchestrState): OrchestrState {
  const rs = state.rippleStates[rippleId];
  const r = state.ripples[rippleId];
  if (!rs || !r) return state;
  let ns = rs.hasPull ? state : setRipple(state, rippleId, { hasPull: true });
  const pf = parentsFreshness(r, ns, now);
  if (pf <= rs.F) {
    // Can't run yet — wake stale parents (those at or behind my freshness).
    const { required, optional } = parentsOf(r, ns);
    for (const pid of [...required, ...optional]) {
      ns = markEdge(ns, pid, rippleId, 'pull');
      const prs = ns.rippleStates[pid];
      if (prs && !prs.isRunning && !prs.hasPull && prs.F <= rs.F) ns = armPull(pid, now, ns);
    }
  }
  return ns;
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

// Tap / Wave: arm the leaves (recurses up any chain that can't yet run).
export function pullPond(pondId: PondId, now: number, state: OrchestrState): OrchestrState {
  let ns = state;
  for (const leaf of getLeaves(pondId, state.ripples)) ns = armPull(leaf.id, now, ns);
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

  // Resupply order: re-arm parents so the pull keeps flowing one layer up. armPull is shallow
  // when the parent can run (back-pressure), but propagates further when the parent needs fresher
  // input — so demand never dies at a node that can't currently produce.
  if (wasPull) {
    const { required, optional } = parentsOf(r, ns);
    for (const pid of [...required, ...optional]) {
      ns = markEdge(ns, pid, rippleId, 'pull');
      ns = armPull(pid, now, ns);
    }
  }

  // "A started pond run must complete." When a pond ROOT begins a run, stamp the OTHER ripples
  // in the pond (its intra-pond descendants) with a push target = this run's freshness. The
  // pulls still pace things normally, but once they stop being refreshed the push forces the
  // rest of the pond's ripples to run through to this freshness — so an initiated pond run
  // always drains to its leaves rather than stalling part-way. Crucially we do NOT stamp the
  // root itself: for an inlet pond (single ripple) pf = now, and a self-push would always be
  // satisfiable → the inlet would run forever. The root's own demand is governed by pull only.
  if (isPondRoot(r, state)) {
    for (const other of Object.values(ns.ripples)) {
      if (other.pondId !== r.pondId || other.id === rippleId) continue;
      const ors = ns.rippleStates[other.id];
      if (ors && (ors.hasPush ?? 0) < pf) ns = setRipple(ns, other.id, { hasPush: pf });
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
    const startedF = leaves.length
      ? Math.min(...leaves.map((l) => {
          const lrs = state.rippleStates[l.id];
          return (lrs?.isRunning ? lrs.runFreshness : lrs?.F) ?? 0;
        }))
      : 0;
    const hasPull = leaves.some((l) => state.rippleStates[l.id]?.hasPull);
    const pushes = leaves.map((l) => state.rippleStates[l.id]?.hasPush ?? 0).filter((v) => v > 0);
    const hasPush = pushes.length ? Math.max(...pushes) : null;

    let genStartTimes = prev.genStartTimes;
    let completionTimes = prev.completionTimes;
    let durations = prev.durations;
    // Stamp the start time of any newly-started generation(s) (keyed by gen number).
    if (runsStarted > prev.runsStarted) {
      genStartTimes = { ...genStartTimes };
      for (let g = prev.runsStarted + 1; g <= runsStarted; g++) genStartTimes[g] = now;
    }
    // On completion, measure latency against THIS generation's own start (not the latest start —
    // they differ once the pond pipelines). Then drop start stamps for consumed generations.
    if (runsCompleted > prev.runsCompleted) {
      completionTimes = pushHistory(prev.completionTimes, now);
      const startedAt = genStartTimes[runsCompleted];
      if (startedAt != null) durations = pushHistory(prev.durations, now - startedAt);
      genStartTimes = { ...genStartTimes };
      for (const g of Object.keys(genStartTimes)) {
        if (Number(g) <= runsCompleted) delete genStartTimes[Number(g)];
      }
    }
    pondStates[pond.id] = { F, startedF, hasPull, hasPush, runsStarted, runsCompleted, genStartTimes, completionTimes, durations };
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

  // 2. Wave = a zero-duration pseudo-ripple consuming the pond's leaves, permanently demanding.
  //    Modelling it as ONE consumer (not re-arming each leaf independently) is what throttles
  //    parallel leaves together: the pseudo-ripple "runs" only when ALL leaves are fresher than
  //    what it last consumed (min over leaves > consumedF), and on each consume it advances its
  //    freshness and re-arms every leaf in lockstep. A fast leaf therefore can't get ahead — its
  //    next pull doesn't arrive until the slowest leaf has also produced and the consumer advances.
  for (const [pondId, trig] of Object.entries(ns.triggers)) {
    if (trig.kind !== 'wave') continue;
    const leaves = getLeaves(pondId, ns.ripples);
    if (leaves.length === 0) continue;
    const consumedF = trig.consumedF ?? -1;
    const leavesF = Math.min(...leaves.map((l) => ns.rippleStates[l.id]?.F ?? 0));
    // Initial kick (consumer has never consumed) OR all leaves advanced past the last consume →
    // the pseudo-ripple consumes this generation and re-arms the leaves for the next.
    if (trig.consumedF === undefined || leavesF > consumedF) {
      ns = { ...ns, triggers: { ...ns.triggers, [pondId]: { ...trig, consumedF: leavesF } } };
      for (const leaf of leaves) ns = armPull(leaf.id, now, ns);
    }
  }

  // 3. Pipelining (standing-demand only): a ripple that is RUNNING and still has standing pull
  //    will need fresh input for its NEXT run. While it's busy it can't re-arm its parents (that
  //    happens on start), so a slow bottleneck leaf would finish and find stale inputs, stalling
  //    a beat each cycle. Re-arm such parents so they produce the next generation in parallel —
  //    but only ONE generation ahead (parent's output no newer than what the busy child is
  //    currently consuming) to bound the buffer and keep back-pressure. This only fires under a
  //    standing trigger (Wave/Tide): a one-shot Tap must NOT pipeline (no standing consumer), or
  //    it would keep producing forever. Gate: the running ripple feeds an active trigger pond.
  if (Object.keys(ns.triggers).length > 0) {
    for (const [id, rs] of Object.entries(ns.rippleStates)) {
      if (!rs.isRunning || !rs.hasPull) continue;
      const r = ns.ripples[id];
      if (!r || !feedsActiveTrigger(r, ns)) continue;
      const consuming = rs.runFreshness ?? rs.F; // freshness this child's current run is built on
      const { required, optional } = parentsOf(r, ns);
      for (const pid of [...required, ...optional]) {
        const prs = ns.rippleStates[pid];
        if (prs && !prs.isRunning && !prs.hasPull && prs.F <= consuming) {
          ns = markEdge(ns, pid, id, 'pull');
          ns = armPull(pid, now, ns);
        }
      }
    }
  }

  // 4. start everything runnable (push targets were propagated eagerly at receive time)
  for (const id of Object.keys(ns.rippleStates)) {
    if (canRun(id, ns, now)) ns = startRipple(id, now, ns);
  }

  // 5. Clear DEAD pull. A ripple can be left holding hasPull it can never act on: it's idle, can't
  //    run (no fresher input), and no parent is running or armed to ever make it fresher. This
  //    happens when a child re-arms a parent that has already caught up to its own inputs (the
  //    resupply is satisfied in place). Drop it, else the pond shows "queued" forever. Inlets are
  //    never dead (their input is always `now`), so they're implicitly excluded by the can-run
  //    check (parentsFreshness = now > F).
  for (const [id, rs] of Object.entries(ns.rippleStates)) {
    if (!rs.hasPull || rs.isRunning) continue;
    const r = ns.ripples[id];
    if (!r) continue;
    if (parentsFreshness(r, ns, now) > rs.F) continue; // can still run — keep
    const { required, optional } = parentsOf(r, ns);
    const parents = [...required, ...optional];
    if (parents.length === 0) continue; // inlet — never dead
    const producerComing = parents.some((pid) => {
      const prs = ns.rippleStates[pid];
      return prs && (prs.isRunning || prs.hasPull);
    });
    if (!producerComing) ns = setRipple(ns, id, { hasPull: false });
  }

  // 6. refresh derived pond rollups
  ns = recomputePonds(ns, now);
  return ns;
}
