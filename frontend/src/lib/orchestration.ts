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
  LogEntry,
} from './types';

// ─── Freshness orchestrator (faithful port of docs/guide/theory.md) ───────────
//
// Ponds and Ripples are first-class state machines. Demand changes (pull/push/stop)
// cascade SYNCHRONOUSLY to a fixpoint at the moment they happen — `tick` only advances
// time: it completes elapsed runs, services Wave/Tide triggers, then starts everything
// runnable. There are no per-tick cleanup/scan passes. See theory.md "Pond State Variables".
//
// Freshness `F` is a wall-clock timestamp (ms). An Inlet mints `now`, or — with a window —
// the window's end ("fresh until"). Everyone else inherits min(required parents) / max(optional).

export interface OrchestrState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  edgeKinds: EdgeKindMap;
  triggers: Record<PondId, ActiveTrigger>;
}

const MAX_HISTORY = 500;
function pushHistory(arr: number[], v: number): number[] {
  const next = [...arr, v];
  return next.length > MAX_HISTORY ? next.slice(next.length - MAX_HISTORY) : next;
}

// ─── Event log ────────────────────────────────────────────────────────────────
// The engine appends events here as they happen; the store drains them after each call so they
// can be shown in the console panel. `s` carries readable names for the message.

let logBuffer: LogEntry[] = [];
let logNow = 0; // wall-clock of the operation currently being processed (for timestamps)

export function drainLog(): LogEntry[] {
  const out = logBuffer;
  logBuffer = [];
  return out;
}

function emit(kind: string, msg: string): void {
  logBuffer.push({ t: logNow, kind, msg });
}
function pname(s: OrchestrState, pid: PondId): string {
  return s.ponds[pid]?.name ?? pid;
}
function rfullname(s: OrchestrState, rid: RippleId): string {
  const r = s.ripples[rid];
  return r ? `${pname(s, r.pondId)}.${r.name}` : rid;
}
// A freshness timestamp rendered as an age in seconds relative to `ref` (0 → "—").
function ageStr(F: number, ref: number): string {
  if (!F) return '—';
  return `${((ref - F) / 1000).toFixed(1)}s`;
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

// Work on a shallow-cloned draft so cascades can mutate in place; the store replaces the slice.
function clone(s: OrchestrState): OrchestrState {
  const pondStates: Record<PondId, PondRunState> = {};
  for (const [k, v] of Object.entries(s.pondStates)) pondStates[k] = { ...v };
  const rippleStates: Record<RippleId, RippleRunState> = {};
  for (const [k, v] of Object.entries(s.rippleStates)) rippleStates[k] = { ...v };
  return { ...s, pondStates, rippleStates, edgeKinds: { ...s.edgeKinds }, triggers: { ...s.triggers } };
}

// ─── Topology helpers ─────────────────────────────────────────────────────────

function ripplesOf(s: OrchestrState, pid: PondId): RippleId[] {
  return Object.values(s.ripples).filter((r) => r.pondId === pid).map((r) => r.id);
}
function intraParents(s: OrchestrState, rid: RippleId): RippleId[] {
  const r = s.ripples[rid];
  return r ? r.parents.filter((p) => s.ripples[p]?.pondId === r.pondId) : [];
}
function leavesOf(s: OrchestrState, pid: PondId): RippleId[] {
  const inPond = ripplesOf(s, pid);
  const childIds = new Set<RippleId>();
  for (const id of inPond) for (const p of intraParents(s, id)) childIds.add(p);
  return inPond.filter((id) => !childIds.has(id));
}
function rootsOf(s: OrchestrState, pid: PondId): RippleId[] {
  return ripplesOf(s, pid).filter((id) => intraParents(s, id).length === 0);
}
function anyRippleBusy(s: OrchestrState, pid: PondId): boolean {
  return ripplesOf(s, pid).some((id) => s.rippleStates[id].isRunning);
}

// ─── Edge marking (visuals only) ──────────────────────────────────────────────

function setEdge(s: OrchestrState, key: string, kind: EdgeDemandKind): void {
  s.edgeKinds[key] = kind;
}
function markRippleEdge(s: OrchestrState, parent: RippleId, child: RippleId, kind: EdgeDemandKind): void {
  const p = s.ripples[parent];
  const c = s.ripples[child];
  if (!p || !c) return;
  setEdge(s, p.pondId === c.pondId ? `${parent}::${child}` : `${p.pondId}::${c.pondId}`, kind);
}
function markPondEdge(s: OrchestrState, src: PondId, sink: PondId, kind: EdgeDemandKind): void {
  setEdge(s, `${src}::${sink}`, kind);
}

// ─── Freshness derivation ─────────────────────────────────────────────────────

// A Pond's source freshness, with the window delay it carries. `F` is null for an Inlet that
// is between windows (cannot run). For a windowed Inlet, F = the current window's end ("fresh
// until") and D = the window's duration. Windows repeat every minute, in seconds [start, end).
function pondSourceF(s: OrchestrState, pid: PondId, now: number): { F: number | null; D: number } {
  const pond = s.ponds[pid];
  if (pond.sources.length === 0) {
    const windows = pond.windows ?? [];
    if (windows.length > 0) {
      const minuteStart = now - (now % 60000);
      const sec = (now % 60000) / 1000;
      const w = windows.find((win) => win.startSec <= sec && sec < win.endSec);
      if (w) return { F: minuteStart + w.endSec * 1000, D: (w.endSec - w.startSec) * 1000 };
      return { F: null, D: 0 };
    }
    return { F: now, D: 0 }; // live source
  }
  const optSrc = new Set(pond.optionalSources ?? []);
  const required = pond.sources.filter((sp) => !optSrc.has(sp));
  if (required.length > 0) return { F: Math.min(...required.map((sp) => s.pondStates[sp]?.endF ?? 0)), D: 0 };
  return { F: Math.max(...pond.sources.map((sp) => s.pondStates[sp]?.endF ?? 0)), D: 0 };
}

function rippleSourceF(s: OrchestrState, rid: RippleId): number {
  const intra = intraParents(s, rid);
  if (intra.length === 0) return s.pondStates[s.ripples[rid].pondId].startF; // root
  const opt = new Set(s.ripples[rid].optionalParents ?? []);
  const req = intra.filter((p) => !opt.has(p));
  if (req.length > 0) return Math.min(...req.map((p) => s.rippleStates[p].endF));
  return Math.max(...intra.map((p) => s.rippleStates[p].endF));
}

// ─── Demand reactions (synchronous cascades) ──────────────────────────────────

// on Pond.hasReceivedPull becomes true
function pondReceivePull(s: OrchestrState, pid: PondId, now: number): void {
  const ps = s.pondStates[pid];
  if (ps.startF === ps.endF) {
    // cold start: wake the whole Pond
    pondSetHasPull(s, pid, now);
    for (const r of ripplesOf(s, pid)) rippleSetHasPull(s, r, now);
  } else {
    // running: only sustain the leaves
    for (const l of leavesOf(s, pid)) rippleSetHasPull(s, l, now);
  }
}

// on Pond.hasPull becomes true
function pondSetHasPull(s: OrchestrState, pid: PondId, now: number): void {
  const ps = s.pondStates[pid];
  if (ps.hasPull) return;
  ps.hasPull = true;
  emit('pond-pull', `${pname(s, pid)} pond gained pull`);
  for (const sp of s.ponds[pid].sources) {
    // Cold-start propagation upstream: wake a Source only when its *startF* equals ours. When the
    // Source is idle, startF == endF, so this is the "caught up to us" case. When it is running,
    // startF > endF >= ours, so the comparison fails — its in-flight Run will deliver fresh output
    // and satisfy this demand, so re-arming it now (a redundant extra generation) is suppressed.
    // This single comparison folds in the "not already producing" check (it leaked a downstream
    // Wave's re-tap into a Pond mid-cycle → inlet over-pull).
    if (s.pondStates[sp].startF === ps.startF) {
      markPondEdge(s, sp, pid, 'pull');
      pondReceivePull(s, sp, now);
    }
  }
}

// on Ripple.hasPull becomes true
function rippleSetHasPull(s: OrchestrState, rid: RippleId, now: number): void {
  const rs = s.rippleStates[rid];
  if (rs.hasPull) return;
  rs.hasPull = true;
  emit('ripple-pull', `${rfullname(s, rid)} gained pull`);
  const intra = intraParents(s, rid);
  if (intra.length === 0) {
    // Root: transfer the pull to the Pond, then drop it from self. The Pond accumulates pull
    // from every root before committing a Run (see canStartPond) — so a Run can't start off
    // the first root's pull and miss a sibling root that registers later in the cascade.
    pondSetHasPull(s, s.ripples[rid].pondId, now);
    rs.hasPull = false;
  } else {
    for (const p of intra) {
      // Same rule between Ripples: a parent's startF equals ours only when it is idle (startF==endF)
      // and started on the same work we did — never while it is running (startF > endF). So this
      // both does the cold-start propagation and suppresses re-arming an already-running parent.
      if (s.rippleStates[p].startF === rs.startF) {
        markRippleEdge(s, p, rid, 'pull'); // cold-start propagation between Ripples
        rippleSetHasPull(s, p, now);
      }
    }
  }
}

// on Pond.targetF changes (push)
function pondSetTarget(s: OrchestrState, pid: PondId, T: number): void {
  const ps = s.pondStates[pid];
  if (ps.targetF != null && ps.targetF >= T) return;
  ps.targetF = T;
  emit('pond-push', `${pname(s, pid)} pond push target → age ${ageStr(T, logNow)}`);
  for (const sp of s.ponds[pid].sources) {
    const sps = s.pondStates[sp];
    if (sps.targetF == null || sps.targetF < T) {
      markPondEdge(s, sp, pid, 'push');
      pondSetTarget(s, sp, T);
    }
  }
}

// ─── Lifecycle ────────────────────────────────────────────────────────────────

function canStartPond(s: OrchestrState, pid: PondId, now: number): boolean {
  const ps = s.pondStates[pid];
  const { F } = pondSourceF(s, pid, now);
  if (F == null) return false;
  // Wait until every root has transferred its pull to the Pond (none still holding) — otherwise
  // a Run could commit off one root's pull and leave a sibling root's demand for the next gen.
  if (rootsOf(s, pid).some((r) => s.rippleStates[r].hasPull)) return false;
  if (ps.targetF != null && F >= ps.targetF) return true;
  return ps.hasPull && F > ps.startF;
}

function startPondRun(s: OrchestrState, pid: PondId, now: number): void {
  const ps = s.pondStates[pid];
  const { F, D: windowD } = pondSourceF(s, pid, now);
  const sourceF = F as number;
  const startedAsPull = ps.hasPull;
  // Did push trigger this run (target now met)? Only then do we stamp the Ripples' targetF.
  const startedAsPush = ps.targetF != null && sourceF >= ps.targetF;

  ps.startF = sourceF;
  ps.hasPull = false;
  if (ps.targetF != null && ps.targetF <= ps.startF) ps.targetF = null;

  // Window delay: from the window for an Inlet, else the worst-case of the deciding Sources.
  if (s.ponds[pid].sources.length === 0) {
    ps.D = windowD;
  } else {
    const ds = s.ponds[pid].sources
      .map((sp) => s.pondStates[sp])
      .filter((sps) => sps.endF === ps.startF)
      .map((sps) => sps.D);
    if (ds.length) ps.D = Math.max(...ds);
  }

  // Stamp every Ripple's targetF ONLY on a push run (drives the whole Pond to the target).
  // Dropped for pull runs for now (diagnostic) — pull runs are governed by pull tokens alone.
  if (startedAsPush) {
    for (const r of ripplesOf(s, pid)) s.rippleStates[r].targetF = ps.startF;
  }

  ps.runsStarted += 1;
  ps.genStartTimes = { ...ps.genStartTimes, [ps.runsStarted]: now };
  emit('pond-start', `${pname(s, pid)} pond run #${ps.runsStarted} started (${startedAsPush ? 'push' : 'pull'}, freshness age ${ageStr(ps.startF, now)})`);

  // Roots transferred their pull to the Pond (rippleSetHasPull) and cleared self; now that the
  // Pond has committed this generation, hand the pull back so every root executes it.
  if (startedAsPull) {
    for (const r of rootsOf(s, pid)) s.rippleStates[r].hasPull = true;
  }

  // A Sink starting as pull replenishes its Sources (Kanban draw → restock).
  if (startedAsPull) {
    for (const sp of s.ponds[pid].sources) {
      markPondEdge(s, sp, pid, 'pull');
      pondReceivePull(s, sp, now);
    }
  }
}

function canStartRipple(s: OrchestrState, rid: RippleId): boolean {
  const rs = s.rippleStates[rid];
  if (rs.isRunning) return false;
  const sourceF = rippleSourceF(s, rid);
  if (rs.targetF != null && sourceF >= rs.targetF) return true;
  return rs.hasPull && sourceF > rs.startF;
}

function startRipple(s: OrchestrState, rid: RippleId, now: number): void {
  const rs = s.rippleStates[rid];
  const r = s.ripples[rid];
  const sourceF = rippleSourceF(s, rid);
  rs.startF = sourceF;
  rs.isRunning = true;
  rs.runStartedAt = now;
  rs.currentRunDurationMs = sampleDuration(r.durationMs, r.variability);
  rs.runsStarted += 1;
  emit('ripple-start', `${rfullname(s, rid)} started (freshness age ${ageStr(sourceF, now)}, ~${((rs.currentRunDurationMs ?? 0) / 1000).toFixed(1)}s)`);

  if (rs.hasPull) {
    const intra = intraParents(s, rid);
    for (const p of intra) {
      markRippleEdge(s, p, rid, 'pull'); // pull propagation upstream
      rippleSetHasPull(s, p, now);
    }
    rs.hasPull = false;
  }
  if (rs.targetF != null && rs.targetF <= sourceF) rs.targetF = null;
}

// Returns true if completing this Ripple completed a Pond Run (Pond.endF advanced).
function completeRipple(s: OrchestrState, rid: RippleId, now: number): boolean {
  const rs = s.rippleStates[rid];
  rs.endF = rs.startF;
  rs.isRunning = false;
  rs.runStartedAt = null;
  rs.lastDurationMs = rs.currentRunDurationMs ?? rs.lastDurationMs;
  rs.currentRunDurationMs = null;
  rs.runsCompleted += 1;
  rs.completionTimes = pushHistory(rs.completionTimes, now);
  rs.durations = pushHistory(rs.durations, rs.lastDurationMs ?? 0);

  emit('ripple-done', `${rfullname(s, rid)} completed (freshness age ${ageStr(rs.endF, now)})`);

  const pid = s.ripples[rid].pondId;
  const ps = s.pondStates[pid];
  const newEnd = Math.min(...leavesOf(s, pid).map((l) => s.rippleStates[l].endF));
  if (newEnd > ps.endF) {
    ps.endF = newEnd;
    ps.runsCompleted += 1;
    ps.completionTimes = pushHistory(ps.completionTimes, now);
    const started = ps.genStartTimes[ps.runsCompleted];
    if (started != null) ps.durations = pushHistory(ps.durations, now - started);
    const gst = { ...ps.genStartTimes };
    delete gst[ps.runsCompleted];
    ps.genStartTimes = gst;
    emit('pond-done', `${pname(s, pid)} pond run #${ps.runsCompleted} completed (freshness age ${ageStr(ps.endF, now)})`);
    return true;
  }
  return false;
}

// ─── Public entry points (operate on a clone, run cascades) ───────────────────

export function tapPond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('tap', `Tap on ${pname(s, pid)}`);
  pondReceivePull(s, pid, now);
  return s;
}

export function pulsePond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('pulse', `Pulse on ${pname(s, pid)}`);
  pondSetTarget(s, pid, now);
  return s;
}

// Greedy stop (simple for now): clear all pull/push demand up the whole ancestry. In-flight
// runs are left to drain. Stops all upstream paths even where other consumers are still queued.
export function stopPond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('stop', `Stop on ${pname(s, pid)} (greedy upstream)`);
  const seen = new Set<PondId>();
  const queue = [pid];
  while (queue.length) {
    const cur = queue.shift()!;
    if (seen.has(cur)) continue;
    seen.add(cur);
    const ps = s.pondStates[cur];
    ps.hasPull = false;
    ps.hasReceivedPull = false;
    ps.targetF = null;
    for (const r of ripplesOf(s, cur)) {
      s.rippleStates[r].hasPull = false;
      s.rippleStates[r].targetF = null;
    }
    for (const sp of s.ponds[cur].sources) {
      markPondEdge(s, sp, cur, 'stop');
      if (!seen.has(sp)) queue.push(sp);
    }
  }
  return s;
}

// ─── Tick (advance time only) ─────────────────────────────────────────────────

export function tick(now: number, stateIn: OrchestrState): OrchestrState {
  logNow = now;
  const s = clone(stateIn);

  // 1. Complete elapsed runs.
  const completedPonds = new Set<PondId>();
  for (const id of Object.keys(s.rippleStates)) {
    const rs = s.rippleStates[id];
    if (rs.isRunning && rs.runStartedAt != null) {
      const dur = rs.currentRunDurationMs ?? s.ripples[id].durationMs;
      if (now - rs.runStartedAt >= dur) {
        if (completeRipple(s, id, now)) completedPonds.add(s.ripples[id].pondId);
      }
    }
  }

  // 2. Standing triggers (modelled as a Sink re-asserting demand).
  for (const [pid, trig] of Object.entries(s.triggers)) {
    const ps = s.pondStates[pid];
    if (trig.kind === 'wave') {
      // Wave re-Taps each time its Pond completes a run, and whenever the Pond sits fully idle.
      const idle = ps.startF === ps.endF && !ps.hasPull && ps.targetF == null && !anyRippleBusy(s, pid);
      if (completedPonds.has(pid) || idle) pondReceivePull(s, pid, now);
    } else {
      // Tide: maintain a maximum staleness by issuing a Pulse to `now` when it is exceeded — but
      // only once no Pulse is already in flight (targetF == null), so the target doesn't outrun
      // supply by being re-bumped every tick. The pending push runs to completion, then the next
      // staleness breach issues a fresh one.
      const staleness = now + ps.D - ps.endF;
      if (ps.targetF == null && (ps.endF === 0 || staleness > (trig.stalenessMs ?? 0))) {
        pondSetTarget(s, pid, now);
      }
    }
  }

  // 3. Start everything runnable, to a fixpoint (a start can cascade demand that enables more).
  let changed = true;
  while (changed) {
    changed = false;
    for (const pid of Object.keys(s.pondStates)) {
      if (canStartPond(s, pid, now)) {
        startPondRun(s, pid, now);
        changed = true;
      }
    }
    for (const rid of Object.keys(s.rippleStates)) {
      if (canStartRipple(s, rid)) {
        startRipple(s, rid, now);
        changed = true;
      }
    }
  }

  return s;
}
