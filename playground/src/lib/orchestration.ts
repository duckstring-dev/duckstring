import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  ActiveTrigger,
  LogEntry,
  Window,
  Weekday,
} from './types';

// "-Inf" freshness sentinel: a push target below every real timestamp (and below the 0 "never run"
// freshness). Used by `force` — a target that is always satisfiable, so the Pond runs once against
// whatever input it currently has. Mirrors the backend's NEVER (datetime.min).
export const NEVER = -Infinity;

// ─── Freshness orchestrator (faithful port of docs/docs/theory.md) ────────────
//
// Ponds and Ripples are first-class state machines. Demand changes (pull/push/sleep)
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
  for (const [k, v] of Object.entries(s.pondStates)) pondStates[k] = { ...v, targets: [...v.targets] };
  const rippleStates: Record<RippleId, RippleRunState> = {};
  for (const [k, v] of Object.entries(s.rippleStates)) rippleStates[k] = { ...v, targets: [...v.targets] };
  return { ...s, pondStates, rippleStates, triggers: { ...s.triggers } };
}

// ─── Push target sets ─────────────────────────────────────────────────────────
// The freshest pending push target, for display/clock reference (null if none).
function maxTarget(targets: number[]): number | null {
  return targets.length ? Math.max(...targets) : null;
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
function anyRippleBusy(s: OrchestrState, pid: PondId): boolean {
  return ripplesOf(s, pid).some((id) => s.rippleStates[id].isRunning);
}

// ─── Windows (RFC-5545-flavoured recurrence; faithful port of engine/core.py Window) ──────────

const UNIT_MS: Record<string, number> = { SECOND: 1000, MINUTE: 60000, HOUR: 3600000, DAY: 86400000, WEEK: 604800000 };
const WEEKDAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

// The recurrence step in ms.
export function windowDelta(w: Window): number {
  return UNIT_MS[w.freqUnit] * w.freqInterval;
}
// Weekday index with Monday = 0 (matches Python's datetime.weekday()); JS getDay() has Sunday = 0.
function weekdayMon0(ms: number): number {
  return (new Date(ms).getDay() + 6) % 7;
}
function windowKept(w: Window, t: number): boolean {
  if (w.until != null && t > w.until) return false;
  return !w.validDays || w.validDays.length === 0 || w.validDays.includes(WEEKDAYS[weekdayMon0(t)]);
}
// If `now` falls inside an occurrence, return that window's end ("fresh until"); else null. O(1):
// the most-recent grid point at/before `now` (windows are assumed non-overlapping).
function windowActiveEnd(w: Window, now: number): number | null {
  const delta = windowDelta(w);
  if (now < w.startAnchor || delta <= 0) return null;
  const c = w.startAnchor + Math.floor((now - w.startAnchor) / delta) * delta;
  if (!windowKept(w, c)) return null;
  const end = c + w.durationMs;
  return now < end ? end : null;
}

// ─── Freshness derivation ─────────────────────────────────────────────────────

// A Pond's source freshness, with the window delay it carries. `F` is null for an Inlet that
// is between windows (cannot run). For a windowed Inlet, F = the soonest-ending active window's end
// ("fresh until") and D = that window's duration.
function pondSourceF(s: OrchestrState, pid: PondId, now: number): { F: number | null; D: number } {
  const pond = s.ponds[pid];
  if (pond.sources.length === 0) {
    const windows = pond.windows ?? [];
    if (windows.length > 0) {
      let best: { F: number; D: number } | null = null;
      for (const w of windows) {
        const end = windowActiveEnd(w, now);
        if (end != null && (best == null || end < best.F)) best = { F: end, D: w.durationMs };
      }
      return best ?? { F: null, D: 0 };
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
  if (ps.isBlocked) return; // a blocked Pond solicits nothing new; it only drains existing demand
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
    // Cold-start propagation upstream: wake any Source that has not started work ahead of us
    // (Source.startF <= our startF). An idle Source has startF == endF, so this covers both the
    // caught-up case (==) and a lagging Source that is behind (<). A Source already running ahead
    // has startF > our startF, so it is skipped — its in-flight Run will deliver fresh output and
    // satisfy this demand, so re-arming it (a redundant extra generation) is suppressed. (That
    // skip is what stopped a downstream Wave's re-tap leaking into a Pond mid-cycle → over-pull.)
    if (s.pondStates[sp].startF <= ps.startF) {
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
    pondSetHasPull(s, s.ripples[rid].pondId, now); // root → lets the Pond start a Run as pull
  } else {
    for (const p of intra) {
      // Cold-start propagation between Ripples: wake any parent that has not started work ahead of
      // us (Parent.startF <= our startF). Idle → startF == endF (caught up or behind); a parent
      // running ahead has startF > ours and is skipped, its in-flight Run satisfying the demand.
      if (s.rippleStates[p].startF <= rs.startF) {
        rippleSetHasPull(s, p, now);
      }
    }
  }
}

// A Pond receives a push target T: record it (if unsatisfied and new) and propagate eagerly
// upstream to required Sources. The set keeps every outstanding request, not just the latest.
function pondAddTarget(s: OrchestrState, pid: PondId, T: number): void {
  const ps = s.pondStates[pid];
  if (ps.isBlocked) return; // no new push enters a blocked Pond (and none propagates upstream from it)
  if (T <= ps.endF || ps.targets.includes(T)) return; // already satisfied, or already requested
  ps.targets.push(T);
  emit('pond-push', `${pname(s, pid)} pond push target → age ${ageStr(T, logNow)}`);
  for (const sp of s.ponds[pid].sources) {
    pondAddTarget(s, sp, T);
  }
}

// A Ripple receives a push target T (from the Pond's run-start stamp). The Pond stamps every
// Ripple, so this records the target without propagating further between Ripples.
function rippleAddTarget(s: OrchestrState, rid: RippleId, T: number): void {
  const rs = s.rippleStates[rid];
  if (T <= rs.endF || rs.targets.includes(T)) return;
  rs.targets.push(T);
}

// ─── Blocked propagation (port of engine/catchment.py derive_blocked) ─────────
// Without failures in the sim, Kill is the only blocking event; the derivation is the same.

// Ponds that depend on `pid` as a *required* Source (an optional Source never blocks a Sink).
function requiredSinks(s: OrchestrState, pid: PondId): PondId[] {
  return Object.values(s.ponds)
    .filter((q) => q.sources.includes(pid) && !(q.optionalSources ?? []).includes(pid))
    .map((q) => q.id);
}

// Recompute `isBlocked` from this Pond's own kill and its required Sources, and — only if it
// changed — propagate to the Sinks so they re-derive.
function deriveBlocked(s: OrchestrState, pid: PondId): void {
  const ps = s.pondStates[pid];
  const pond = s.ponds[pid];
  const optSrc = new Set(pond.optionalSources ?? []);
  const blocked =
    ps.isKilled ||
    pond.sources.some((sp) => !optSrc.has(sp) && (s.pondStates[sp].isBlocked || s.pondStates[sp].isKilled));
  if (blocked !== ps.isBlocked) {
    ps.isBlocked = blocked;
    for (const q of requiredSinks(s, pid)) deriveBlocked(s, q);
  }
}

// ─── Lifecycle ────────────────────────────────────────────────────────────────

function canStartPond(s: OrchestrState, pid: PondId, now: number): boolean {
  const ps = s.pondStates[pid];
  if (ps.isKilled) return false; // terminal until an operator Wake/Force
  const { F } = pondSourceF(s, pid, now);
  if (F == null) return false;
  // Push: run when the inputs can satisfy the oldest outstanding request (the run takes the freshest
  // input, so it satisfies every target it has reached). The set lets a pipelined Tide's earlier
  // targets be served in turn instead of being lost behind a moving `now`.
  if (ps.targets.length && F >= Math.min(...ps.targets)) return true;
  return ps.hasPull && F > ps.startF; // pull with fresher input
}

function startPondRun(s: OrchestrState, pid: PondId, now: number): void {
  const ps = s.pondStates[pid];
  const { F, D: windowD } = pondSourceF(s, pid, now);
  const sourceF = F as number;
  const startedAsPull = ps.hasPull;
  const startedAsPush = ps.targets.length > 0 && sourceF >= Math.min(...ps.targets);

  // A Sink starting as pull replenishes all its Sources (Kanban draw → restock); no-op for an
  // Inlet. Done before clearing hasPull, matching "if hasPull and not Inlet" in theory. A blocked
  // Pond drains but never solicits its Sources; a Wake (pullLocal) also doesn't propagate.
  if (startedAsPull && !ps.isBlocked && !ps.pullLocal) {
    for (const sp of s.ponds[pid].sources) {
      pondReceivePull(s, sp, now);
    }
  }

  ps.startF = sourceF;
  ps.hasPull = false;
  ps.pullLocal = false;
  ps.targets = ps.targets.filter((t) => t > ps.startF); // this Run satisfies every target it reached

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

  // Every Ripple in the Pond Run must reach this freshness — stamped on ALL Ripples on every Run
  // (pull or push), so the whole Pond always executes to completion (push-style). This also initiates
  // the run: roots have sourceF == startF, satisfying the target. (theory.md "send target startF")
  for (const r of ripplesOf(s, pid)) rippleAddTarget(s, r, ps.startF);

  ps.runsStarted += 1;
  ps.genStartTimes = { ...ps.genStartTimes, [ps.runsStarted]: now };
  emit('pond-start', `${pname(s, pid)} pond run #${ps.runsStarted} started (${startedAsPush ? 'push' : 'pull'}, freshness age ${ageStr(ps.startF, now)})`);
}

function canStartRipple(s: OrchestrState, rid: RippleId): boolean {
  const rs = s.rippleStates[rid];
  if (rs.isRunning) return false;
  const sourceF = rippleSourceF(s, rid);
  if (rs.targets.length && sourceF >= Math.min(...rs.targets)) return true;
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
    for (const p of intraParents(s, rid)) {
      rippleSetHasPull(s, p, now); // pull propagation upstream
    }
    rs.hasPull = false;
  }
  rs.targets = rs.targets.filter((t) => t > sourceF); // this Run satisfies every target it reached
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
  pondAddTarget(s, pid, now);
  return s;
}

// Sleep a Pond: clear its push+pull demand and its Ripples' **pull** demand, but KEEP Ripple push
// targets so any already-started Pond Run completes. With `upstream=true` the sleep propagates to
// every ancestor (a token following the source edges), clearing each one's demand too. The soft
// counterpart to Wake — it lets the Pond settle, vs Kill which cancels everything. Faithful port
// of engine/catchment.py:sleep_pond.
export function sleepPond(state: OrchestrState, pid: PondId, now: number, upstream = false): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('sleep', `Sleep on ${pname(s, pid)}${upstream ? ' (upstream)' : ''}`);
  const seen = new Set<PondId>();
  const queue = [pid];
  while (queue.length) {
    const cur = queue.shift()!;
    if (seen.has(cur)) continue;
    seen.add(cur);
    const ps = s.pondStates[cur];
    ps.hasPull = false;
    ps.hasReceivedPull = false;
    ps.targets = [];
    for (const r of ripplesOf(s, cur)) {
      s.rippleStates[r].hasPull = false; // keep targets (push) so started runs complete
    }
    if (upstream) {
      for (const sp of s.ponds[cur].sources) {
        if (!seen.has(sp)) queue.push(sp);
      }
    }
  }
  return s;
}

// Wake a Pond: a one-shot, **non-propagating** pull. The Pond runs once when its Sources already
// offer something fresher than its last Run (sourceF > startF); it does NOT solicit its Sources
// (no upstream propagation — that's a Tap). Also clears a kill, so a parked Pond resumes. Port of
// engine/catchment.py:wake_pond.
export function wakePond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('wake', `Wake on ${pname(s, pid)} (one-shot pull, no upstream)`);
  clearHalt(s, pid);
  const ps = s.pondStates[pid];
  ps.hasPull = true;
  ps.pullLocal = true;
  return s;
}

// Force a Pond Run now — a recompute even with no upstream change. Resets the Pond's and its
// Ripples' endF so the idempotency guards re-execute them, and injects a one-shot demand (a push
// target of NEVER, always satisfiable). It runs at the **current** freshness, so endF returns
// unchanged and it does **not** propagate downstream. Clears a kill (the operator override).
// Port of engine/catchment.py:force_pond.
export function forcePond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('force', `Force on ${pname(s, pid)} (recompute at current freshness)`);
  clearHalt(s, pid);
  const ps = s.pondStates[pid];
  ps.endF = NEVER;
  for (const r of ripplesOf(s, pid)) s.rippleStates[r].endF = NEVER;
  if (!ps.targets.includes(NEVER)) ps.targets.push(NEVER);
  return s;
}

// Kill a Pond: cancel its in-flight Run and park it in a terminal *killed* state. Clears all
// demand, stops its Ripples, and blocks downstream — it stays down until an operator Wake/Force.
// Port of engine/catchment.py:kill_pond.
export function killPond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('kill', `Kill on ${pname(s, pid)}`);
  const ps = s.pondStates[pid];
  ps.isKilled = true;
  ps.hasPull = false;
  ps.hasReceivedPull = false;
  ps.pullLocal = false;
  ps.targets = [];
  for (const r of ripplesOf(s, pid)) {
    const rs = s.rippleStates[r];
    rs.isRunning = false;
    rs.runStartedAt = null;
    rs.currentRunDurationMs = null;
    rs.hasPull = false;
    rs.targets = [];
  }
  deriveBlocked(s, pid); // killed Pond blocks its Sinks
  return s;
}

// Clear the operator halt on a Pond (kill) and re-derive downstream blocks. Mirrors the backend's
// _clear_halt, minus the failure fields (errors are out of the sim's scope).
function clearHalt(s: OrchestrState, pid: PondId): void {
  const ps = s.pondStates[pid];
  const halted = ps.isKilled;
  ps.isKilled = false;
  // Abandon the halted Run's phantom (startF > endF): without this the Pond would read as
  // perpetually in-flight. Returning startF to endF leaves it genuinely idle, ready to run again.
  if (halted) ps.startF = ps.endF;
  deriveBlocked(s, pid); // may stay blocked if a required Source is still killed/blocked
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
      const idle = ps.startF === ps.endF && !ps.hasPull && ps.targets.length === 0 && !anyRippleBusy(s, pid);
      if (completedPonds.has(pid) || idle) pondReceivePull(s, pid, now);
    } else {
      // Tide: a clock. It adds a fresh push target `now` every time the freshness it last *requested*
      // has itself aged past `limit`. The reference is the newest pending target if any, else `startF`
      // (set to sourceF *before* targets are cleared, so it preserves the satisfied target; reading
      // endF would mistime the clock, as it lags by the lead). Pulses pipeline freely: a limit below
      // the lead time just means several targets are outstanding at once, each served in turn via the
      // push run condition, so completions still land every `limit` (down to the bottleneck).
      const limit = trig.stalenessMs ?? 0;
      const ref = maxTarget(ps.targets) ?? ps.startF;
      if (now + ps.D - ref >= limit) {
        pondAddTarget(s, pid, now);
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
