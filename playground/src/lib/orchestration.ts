import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  Refresh,
  LogEntry,
  Window,
  Weekday,
  ThrottleState,
} from './types';

// "-Inf" freshness sentinel: a push target below every real timestamp (and below the 0 "never run"
// freshness). Used by `force` — a target that is always satisfiable, so the Pond runs once against
// whatever input it currently has. Mirrors the backend's NEVER (datetime.min).
export const NEVER = -Infinity;

// ─── Freshness orchestrator ──────────────────────────────────────────────────
//
// Ponds and Ripples are first-class state machines. Demand is a set of requested freshnesses (targets): a
// Refresh asks a Pond for data at least as fresh as a moment, and the request travels to its Sources. Demand
// changes cascade SYNCHRONOUSLY to a fixpoint at the moment they happen; `tick` only advances time: it
// completes elapsed runs, sends the standing Refreshes that are due, then starts everything runnable. The
// Python engine (engine/catchment.py) is a port of this one.
//
// Freshness `F` is a wall-clock timestamp (ms). An Inlet mints `now`, or — with a window —
// the window's end ("fresh until"). Everyone else inherits min(required parents) / max(optional).

export interface OrchestrState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  refreshes: Record<PondId, Refresh[]>; // standing Refreshes per Pond
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
  for (const [k, v] of Object.entries(s.pondStates)) {
    pondStates[k] = {
      ...v,
      targets: [...v.targets],
      throttle: v.throttle && {
        counts: { ...v.throttle.counts },
        slow: [...v.throttle.slow],
        held: v.throttle.held,
        trigger: v.throttle.trigger,
        forwardedIn: { ...v.throttle.forwardedIn },
        deferred: { ...v.throttle.deferred },
        deferredAt: { ...v.throttle.deferredAt },
        leads: v.throttle.leads && { ...v.throttle.leads },
        seenF: v.throttle.seenF && { ...v.throttle.seenF },
        launchAt: v.throttle.launchAt,
      },
    };
  }
  const rippleStates: Record<RippleId, RippleRunState> = {};
  for (const [k, v] of Object.entries(s.rippleStates)) rippleStates[k] = { ...v, targets: [...v.targets] };
  return { ...s, pondStates, rippleStates, refreshes: { ...s.refreshes } };
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
// A Pond's first Ripples: those with no parent inside the Pond (they read its Sources, or for an Inlet, its data).
function rootsOf(s: OrchestrState, pid: PondId): RippleId[] {
  return ripplesOf(s, pid).filter((r) => intraParents(s, r).length === 0);
}

// ─── Windows (RFC-5545-flavoured recurrence; faithful port of engine/core.py Window) ──────────

const UNIT_MS: Record<string, number> = { SECOND: 1000, MINUTE: 60000, HOUR: 3600000, DAY: 86400000, WEEK: 604800000 };
const WEEKDAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

// The recurrence step in ms.
export function windowDelta(w: Window): number {
  return UNIT_MS[w.freqUnit] * w.freqInterval;
}
// Whether a grid point is kept by an optional weekday filter and end of recurrence (windows and anchored
// Refreshes share these).
function recurrenceKept(validDays: Weekday[] | undefined, until: number | undefined, t: number): boolean {
  if (until != null && t > until) return false;
  return !validDays || validDays.length === 0 || validDays.includes(WEEKDAYS[weekdayMon0(t)]);
}
// Weekday index with Monday = 0 (matches Python's datetime.weekday()); JS getDay() has Sunday = 0.
function weekdayMon0(ms: number): number {
  return (new Date(ms).getDay() + 6) % 7;
}
function windowKept(w: Window, t: number): boolean {
  return recurrenceKept(w.validDays, w.until, t);
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

// A Pond receives a request for freshness T: record it as a target (if unsatisfied and new) and propagate it
// upstream (see `pushSources`), unless the throttle holds it here. The set keeps every outstanding request, not
// just the latest. `fromTrigger` marks a Refresh's own request, which makes this a trigger Pond. Returns whether
// the request was taken.
function pondAddTarget(s: OrchestrState, pid: PondId, T: number, fromTrigger = false): boolean {
  const ps = s.pondStates[pid];
  if (ps.isBlocked) return false; // no new demand enters a blocked Pond (and none propagates upstream from it)
  if (T <= ps.endF || ps.targets.includes(T)) return false; // already satisfied, or already requested
  if (T <= ps.startF) return false; // a Run already in flight at or past T will satisfy it
  ps.targets.push(T);
  emit('pond-demand', `${pname(s, pid)} asked for data ${ageStr(T, logNow)} old`);
  if (fromTrigger) throttleOf(s, pid).trigger = true;
  if (!Number.isFinite(T)) return true; // Force's NEVER is not demand
  countThrottle(s, pid);
  if (holdsDemand(s, pid)) {
    throttleOf(s, pid).held += 1;
    emit('hold', `${pname(s, pid)} holds the demand (${throttleOf(s, pid).held} held)`);
    return true;
  }
  forwardDemand(s, pid, T);
  return true;
}

// The Sources push demand propagates to: the required ones (an optional Source is never waited for, so
// forcing it to a target is pointless), plus, once, an optional Source that has never started a run (a Sink
// would otherwise read output nobody builds). With no required Sources, all of them: the Pond runs on
// whichever is fresher, so it needs them asked.
function pushSources(s: OrchestrState, pid: PondId): PondId[] {
  const pond = s.ponds[pid];
  const opt = new Set(pond.optionalSources ?? []);
  const required = pond.sources.filter((sp) => !opt.has(sp));
  if (required.length === 0) return pond.sources;
  const unbuilt = (sp: PondId) => {
    const sps = s.pondStates[sp];
    return !!sps && sps.runsStarted === 0 && !sps.targets.some((t) => Number.isFinite(t));
  };
  return pond.sources.filter((sp) => !opt.has(sp) || unbuilt(sp));
}

// ─── Throttle ────────────────────────────────────────────────────────────────
//
// Paces demand that arrives faster than the pipeline can serve it. Sparse demand never triggers it.
//
// Detection is per Ripple: a Ripple that sees two demands reach its Pond during one of its runs can't keep
// up, so it is *slow*, until it goes idle (an idle stage isn't the bottleneck: a stage below the bottleneck
// idles every cycle waiting for it, and a stray flag from one long noisy run clears the same way). A Pond
// with a slow Ripple, or a throttled required Source, is *throttled*; that flag is all that passes between
// Ponds (read from Sources, like Blocked, and never across an optional edge).
//
// Two kinds of Pond hold the demand that reaches them, keeping it as their own targets (so they still run
// on anything fresher that arrives) but not passing it upstream straight away:
//   - a throttled trigger Pond (the Outlet; the only reader of the throttled flag), on its completions;
//   - any other Pond with a slow Ripple (the bottleneck), on that Ripple's starts.
// Each sends one request upstream (at `now`) per turn while it has demand to meet. So demand travels upstream
// at the bottleneck's pace, timed to its starts (one generation ahead of it), and enters at the rate the Outlet
// is served, which bounds every path into it, diamonds and siblings included. A trigger Pond never paces on
// its own slow Ripple: it takes raw trigger demand, so under a fast trigger its Ripple always looks slow.

function throttleOf(s: OrchestrState, pid: PondId): ThrottleState {
  const ps = s.pondStates[pid];
  if (!ps.throttle) ps.throttle = { counts: {}, slow: [], held: 0, forwardedIn: {}, deferred: {}, deferredAt: {} };
  return ps.throttle;
}

// Demand reached this Pond: count it against each running Ripple's current run.
function countThrottle(s: OrchestrState, pid: PondId): void {
  const th = throttleOf(s, pid);
  for (const rid of ripplesOf(s, pid)) {
    const rs = s.rippleStates[rid];
    if (!rs.isRunning) continue;
    const c = th.counts[rid]?.run === rs.runsStarted ? th.counts[rid].n : 0;
    th.counts[rid] = { run: rs.runsStarted, n: c + 1 };
    if (c + 1 >= 2 && !th.slow.includes(rid)) {
      th.slow.push(rid);
      emit('throttled', `${rfullname(s, rid)} can't keep up (2 demands in one run): ${pname(s, pid)} is throttled`);
    }
  }
}

// Does this Pond hold the demand reaching it? A trigger Pond while throttled; any other while it has a slow
// Ripple. (A Pond with nowhere to send demand, an Inlet, never holds.)
function holdsDemand(s: OrchestrState, pid: PondId): boolean {
  if (pushSources(s, pid).length === 0) return false;
  const th = s.pondStates[pid].throttle;
  if (!th) return false;
  return th.trigger ? isThrottled(s, pid) : th.slow.length > 0;
}

// Forward demand upstream, noting which run each Ripple was in. Sibling alignment: when some required push
// Sources are throttled and others aren't, T goes to the throttled ones now and is held back from the rest
// until each throttled one has started a new run (its turn), so the Sources read at about the same time and
// the join's freshness isn't dragged down by a fast Source that read early.
function forwardDemand(s: OrchestrState, pid: PondId, T: number): void {
  const th = throttleOf(s, pid);
  th.held = 0;
  for (const rid of ripplesOf(s, pid)) th.forwardedIn[rid] = s.rippleStates[rid].runsStarted;
  const sources = pushSources(s, pid);
  const lead = leadSources(s, pid, sources);
  const hold = lead.length > 0 && lead.length < sources.length;
  if (hold && Object.keys(th.deferred).length === 0) {
    th.deferredAt = Object.fromEntries(lead.map((sp) => [sp, s.pondStates[sp].runsStarted]));
  }
  for (const sp of sources) {
    if (hold && !lead.includes(sp)) {
      if (th.deferred[sp] == null || T > th.deferred[sp]) th.deferred[sp] = T;
      continue;
    }
    if (th.deferred[sp] != null && th.deferred[sp] <= T) delete th.deferred[sp]; // T covers what was held back
    pondAddTarget(s, sp, T);
  }
  if (Object.keys(th.deferred).length === 0) th.deferredAt = {};
}

// The throttled required Sources among `sources`: the ones the others are aligned to.
function leadSources(s: OrchestrState, pid: PondId, sources: PondId[]): PondId[] {
  const opt = new Set(s.ponds[pid].optionalSources ?? []);
  return sources.filter((sp) => !opt.has(sp) && isThrottled(s, sp));
}

// Send each held-back target on once every throttled Source has started a new run since it was held, or once
// none is throttled. A held-back Source that has since become throttled itself gets its target at once; one that
// is no longer a push Source (an optional Source now built) is dropped. Returns whether anything changed.
function flushDeferred(s: OrchestrState, pid: PondId): boolean {
  const th = s.pondStates[pid].throttle;
  if (!th || Object.keys(th.deferred).length === 0) return false;
  const sources = pushSources(s, pid);
  const lead = leadSources(s, pid, sources);
  const due = lead.every((sp) => s.pondStates[sp].runsStarted > (th.deferredAt[sp] ?? -1));
  let changed = false;
  for (const [sp, T] of Object.entries(th.deferred)) {
    if (due || lead.includes(sp) || !sources.includes(sp)) {
      delete th.deferred[sp];
      if (sources.includes(sp)) pondAddTarget(s, sp, T);
      changed = true;
    }
  }
  if (Object.keys(th.deferred).length === 0) th.deferredAt = {};
  return changed;
}

// Is a holding Pond's turn due? A bottleneck's turn: every slow Ripple has started a new run since the last
// send. A trigger Pond's (or a Pond whose slow Ripple has since gone idle): it completed a run since the last
// send (or has never sent), or it is no longer throttled.
function releaseDue(s: OrchestrState, pid: PondId, completed: Set<PondId>): boolean {
  const th = s.pondStates[pid].throttle!;
  if (!th.trigger && th.slow.length > 0) {
    return th.slow.every((rid) => s.rippleStates[rid].runsStarted > (th.forwardedIn[rid] ?? -1));
  }
  // Never sent: no request is outstanding, so there is nothing to wait for (else a trigger Pond already throttled
  // through a Source another Refresh paces would hold its first request until a completion it is needed for).
  const neverSent = Object.keys(th.forwardedIn).length === 0;
  return th.held > 0 && (completed.has(pid) || neverSent || !isThrottled(s, pid));
}

// Throttled: a slow Ripple of its own, or a throttled required Source (with only optional Sources, all of
// them, since it runs on whichever is fresher). Never across an optional edge.
export function isThrottled(s: OrchestrState, pid: PondId): boolean {
  if ((s.pondStates[pid]?.throttle?.slow.length ?? 0) > 0) return true;
  const pond = s.ponds[pid];
  const opt = new Set(pond.optionalSources ?? []);
  const required = pond.sources.filter((sp) => !opt.has(sp));
  if (required.length > 0) return required.some((sp) => isThrottled(s, sp));
  return pond.sources.length > 0 && pond.sources.every((sp) => isThrottled(s, sp));
}

// A Refresh's request at its Pond: marks it a trigger Pond (if taken) until its demand drains.
function triggerDemand(s: OrchestrState, pid: PondId, T: number): boolean {
  return pondAddTarget(s, pid, T, true);
}

// A floating Refresh's reference: the later of its newest pending request and the Pond's freshness. A request is
// only dropped once a run at or past it has started, so this is never earlier than the newest request; and a run
// it didn't ask for (another Refresh, a Force) counts too.
function clockRef(ps: PondRunState): number {
  return Math.max(maxTarget(ps.targets) ?? -Infinity, ps.startF);
}

// The latest kept occurrence of an anchored Refresh at or before `now` (null before its start, or when that grid
// point is filtered out).
function latestOccurrence(r: Refresh, now: number): number | null {
  if (r.startAnchor == null || r.everyMs <= 0 || now < r.startAnchor) return null;
  const c = r.startAnchor + Math.floor((now - r.startAnchor) / r.everyMs) * r.everyMs;
  return recurrenceKept(r.validDays, r.until, c) ? c : null;
}

// The next kept occurrence of an anchored Refresh after `now` (bounded scan for a weekday filter).
function nextOccurrence(r: Refresh, now: number): number | null {
  if (r.startAnchor == null || r.everyMs <= 0) return null;
  let k = now < r.startAnchor ? 0 : Math.floor((now - r.startAnchor) / r.everyMs) + 1;
  for (let i = 0; i < 10000; i++, k++) {
    const t = r.startAnchor + k * r.everyMs;
    if (r.until != null && t > r.until) return null;
    if (recurrenceKept(r.validDays, undefined, t)) return t;
  }
  return null;
}

// When a standing Refresh next sends a request: null for a continuous one (every 0 s) or one with no further
// occurrence. A floating Refresh is due `everyMs` after its clock reference.
export function refreshNextDue(s: OrchestrState, pid: PondId, r: Refresh, now: number): number | null {
  if (r.startAnchor != null) return nextOccurrence(r, now);
  if (r.everyMs <= 0) return null;
  const ps = s.pondStates[pid];
  if (!ps) return null;
  return Math.max(now, clockRef(ps) + r.everyMs - ps.D);
}

// ─── Launch alignment (EXPERIMENT, switchable) ───────────────────────────────
//
// A bottleneck releases on its starts, so the Inlets start at once and their data reaches it after the lead (the
// time from the Inlets' start to its input being ready). With a lead shorter than its run, that data waits for the
// rest of the run; with a lead just over a whole number of runs, nearly a whole run. Launch alignment holds the
// release until the slot the data will feed, minus the lead and a margin, so the data arrives as the bottleneck
// frees. The lead is read from freshness (when an input became ready, less its freshness, the Inlets' start), so
// delaying the release doesn't change it: there is no feedback loop. Estimates are conservative (the longest recent
// lead, the shortest recent run) so data is early rather than late; late data idles the bottleneck only for the
// lateness. Windowed freshness (D > 0) isn't a start time, so it falls back to releasing at once.
export const launchAlign = { on: false, margin: 0.1, window: 4, spread: 0, gate: 0.1 };

// Record each Ripple's lead when its input freshness advances (it became ready now).
function sampleLeads(s: OrchestrState, pid: PondId, now: number): void {
  const ps = s.pondStates[pid];
  const th = throttleOf(s, pid);
  for (const rid of ripplesOf(s, pid)) {
    const f = rippleSourceF(s, rid);
    const seen = th.seenF?.[rid];
    if (seen != null && f <= seen) continue;
    th.seenF = { ...th.seenF, [rid]: f };
    if (seen == null || ps.D > 0 || f <= 0 || now < f) continue;
    th.leads = { ...th.leads, [rid]: [...(th.leads?.[rid] ?? []), now - f].slice(-launchAlign.window) };
  }
}

// When a bottleneck's due release should go out: for each slow Ripple, the first slot time (its run's start plus
// whole recent run times) less the lead and the margin that isn't already past; the earliest over its slow Ripples.
// `now` when there's nothing to go on.
function launchTime(s: OrchestrState, pid: PondId, now: number): number {
  const th = s.pondStates[pid].throttle!;
  let launch = Infinity;
  for (const rid of th.slow) {
    const rs = s.rippleStates[rid];
    const leads = th.leads?.[rid] ?? [];
    const runs = rs.durations.slice(-launchAlign.window);
    if (rs.runStartedAt == null || leads.length === 0 || runs.length === 0) return now;
    const period = Math.min(...runs);
    // Cushion: a share of the run, plus the recent spread of leads and runs (so noise shrinks the delay).
    const spread = Math.max(...leads) - Math.min(...leads) + Math.max(...runs) - period;
    if (spread > launchAlign.gate * period) return now; // not steady enough to time: release as today
    const lead = Math.max(...leads) + launchAlign.margin * period + launchAlign.spread * spread;
    const k = Math.max(1, Math.ceil((now - rs.runStartedAt + lead) / period));
    launch = Math.min(launch, rs.runStartedAt + k * period - lead);
  }
  return Number.isFinite(launch) ? Math.min(Math.max(launch, now), now + Math.max(0, ...th.slow.map((r) => s.rippleStates[r].durations.at(-1) ?? 0))) : now;
}

// After the tick's starts: clear slow Ripples that went idle, then let each Pond holding demand send one
// request upstream if its turn has come. Returns whether anything changed.
function settleThrottle(s: OrchestrState, now: number, completed: Set<PondId>): boolean {
  let changed = false;
  for (const pid of Object.keys(s.pondStates)) {
    const ps = s.pondStates[pid];
    const th = ps.throttle;
    if (!th) continue;
    if (th.slow.length > 0) {
      const idle = th.slow.filter((rid) => !s.rippleStates[rid]?.isRunning);
      if (idle.length > 0) {
        th.slow = th.slow.filter((rid) => !idle.includes(rid));
        for (const rid of idle) emit('unthrottled', `${rfullname(s, rid)} went idle: no longer slow`);
        if (th.slow.length === 0) th.launchAt = null;
        changed = true;
      }
    }
    if (flushDeferred(s, pid)) changed = true;
    if (launchAlign.on) sampleLeads(s, pid, now);
    if (!ps.targets.some((t) => Number.isFinite(t))) {
      th.trigger = false; // demand drained
      th.held = 0;
      th.deferred = {};
      th.deferredAt = {};
      continue;
    }
    if (pushSources(s, pid).length > 0 && releaseDue(s, pid, completed)) {
      if (launchAlign.on && !th.trigger && th.slow.length > 0) {
        if (th.launchAt == null) th.launchAt = launchTime(s, pid, now);
        if (now < th.launchAt) continue;
      }
      th.launchAt = null;
      emit('release', `${pname(s, pid)} sends one request upstream (${th.held} held)`);
      completed.delete(pid);
      forwardDemand(s, pid, now);
      changed = true;
    }
  }
  return changed;
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
    // Unblocked: demand forwarded while a Source was blocked was refused there, so send the newest unmet
    // target up again.
    const unmet = ps.targets.filter((t) => Number.isFinite(t));
    if (!blocked && unmet.length > 0) forwardDemand(s, pid, Math.max(...unmet));
    for (const q of requiredSinks(s, pid)) deriveBlocked(s, q);
  }
}

// ─── Lifecycle ────────────────────────────────────────────────────────────────

function canStartPond(s: OrchestrState, pid: PondId, now: number): boolean {
  const ps = s.pondStates[pid];
  if (ps.isKilled) return false; // terminal until an operator Wake/Force
  // An Inlet waits until its first Ripples (the ones that read) are free, so its freshness stamp matches when
  // it reads rather than when a Pond Run queued. Later Ripples may still be busy; its Runs pipeline through them.
  if (s.ponds[pid].sources.length === 0 && rootsOf(s, pid).some((r) => s.rippleStates[r].isRunning)) {
    return false;
  }
  const { F } = pondSourceF(s, pid, now);
  if (F == null) return false;
  // Push: run when the inputs can satisfy the oldest outstanding request (the run takes the freshest
  // input, so it satisfies every target it has reached). The set lets a pipelined Refresh's earlier
  // targets be served in turn instead of being lost behind a moving `now`.
  return ps.targets.length > 0 && F >= Math.min(...ps.targets);
}

function startPondRun(s: OrchestrState, pid: PondId, now: number): void {
  const ps = s.pondStates[pid];
  const { F, D: windowD } = pondSourceF(s, pid, now);
  const sourceF = F as number;

  ps.startF = sourceF;
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

  // Every Ripple in the Pond Run must reach this freshness: stamped on ALL Ripples on every Run, so the whole
  // Pond always executes to completion. This also initiates the run: roots have sourceF == startF.
  for (const r of ripplesOf(s, pid)) rippleAddTarget(s, r, ps.startF);

  ps.runsStarted += 1;
  ps.startTimesByF = { ...ps.startTimesByF, [ps.startF]: now };
  emit('pond-start', `${pname(s, pid)} pond run #${ps.runsStarted} started (freshness age ${ageStr(ps.startF, now)})`);
}

function canStartRipple(s: OrchestrState, rid: RippleId): boolean {
  const rs = s.rippleStates[rid];
  if (rs.isRunning) return false;
  const sourceF = rippleSourceF(s, rid);
  return rs.targets.length > 0 && sourceF >= Math.min(...rs.targets);
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
    // Runs are matched by freshness, as the Catchment's pond_run rows are. Pairing the Nth completion
    // with the Nth start drifts once a newer Run absorbs an older one in flight (its Ripples skip to
    // the fresher target, so the older Run never completes on its own): every later duration would be
    // measured from an ever-older start. Older entries are dropped as absorbed.
    const started = ps.startTimesByF[newEnd];
    if (started != null) ps.durations = pushHistory(ps.durations, now - started);
    ps.ages = pushHistory(ps.ages ?? [], now - newEnd);
    const pending: Record<number, number> = {};
    for (const [f, t] of Object.entries(ps.startTimesByF)) if (Number(f) > newEnd) pending[Number(f)] = t;
    ps.startTimesByF = pending;
    emit('pond-done', `${pname(s, pid)} pond run #${ps.runsCompleted} completed (freshness age ${ageStr(ps.endF, now)})`);
    return true;
  }
  return false;
}

// ─── Public entry points (operate on a clone, run cascades) ───────────────────

// Refresh now: ask for this Pond's data to be at least as fresh as `now`.
export function refreshNow(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('refresh', `Refresh on ${pname(s, pid)}`);
  triggerDemand(s, pid, now);
  return s;
}

// Sleep a Pond: clear its demand, but KEEP Ripple targets so any already-started Pond Run completes. With
// `upstream=true` the sleep propagates to every ancestor, clearing each one's demand too. The soft counterpart to
// Kill, which cancels everything.
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
    ps.targets = [];
    ps.throttle = undefined;
    if (upstream) {
      for (const sp of s.ponds[cur].sources) {
        if (!seen.has(sp)) queue.push(sp);
      }
    }
  }
  // Demand other Ponds still hold was cleared at the slept Sources they forwarded it to: send each one's newest
  // unmet target to those Sources again, so another consumer's pending request isn't stranded.
  for (const [q, qs] of Object.entries(s.pondStates)) {
    const unmet = qs.targets.filter((t) => Number.isFinite(t));
    if (seen.has(q) || qs.isBlocked || unmet.length === 0) continue;
    const heldBack = qs.throttle?.deferred ?? {};
    for (const sp of pushSources(s, q)) {
      if (seen.has(sp) && heldBack[sp] == null) pondAddTarget(s, sp, Math.max(...unmet));
    }
  }
  return s;
}

// Force a Pond Run now — a recompute even with no upstream change. Resets the Pond's and its
// Ripples' endF so the idempotency guards re-execute them, and injects a one-shot demand (a push
// target of NEVER, always satisfiable). It runs at the **current** freshness, so endF returns
// unchanged and it does **not** propagate downstream. Clears a kill (the operator override).
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
// demand, stops its Ripples, and blocks downstream; it stays down until a Force.
export function killPond(state: OrchestrState, pid: PondId, now: number): OrchestrState {
  logNow = now;
  const s = clone(state);
  emit('kill', `Kill on ${pname(s, pid)}`);
  const ps = s.pondStates[pid];
  ps.isKilled = true;
  ps.targets = [];
  ps.throttle = undefined;
  for (const r of ripplesOf(s, pid)) {
    const rs = s.rippleStates[r];
    rs.isRunning = false;
    rs.runStartedAt = null;
    rs.currentRunDurationMs = null;
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

  // 2. Standing Refreshes. A floating one (no start) sends a request at `now` once `everyMs` has passed since its
  // clock reference (the later of its newest pending request and the Pond's freshness; every 0 s is continuous).
  // Requests pipeline freely: a period below the lead time means several are outstanding at once, each served in
  // turn, and the throttle paces them to the bottleneck. An anchored one (a start time) requests its latest grid
  // occurrence, which the target rules make idempotent, so it fires on its grid whatever else refreshes the Pond.
  for (const [pid, refs] of Object.entries(s.refreshes)) {
    const ps = s.pondStates[pid];
    if (!ps) continue;
    for (const r of refs) {
      if (r.startAnchor != null) {
        const c = latestOccurrence(r, now);
        if (c != null && triggerDemand(s, pid, c)) emit('refresh', `${r.name} on ${pname(s, pid)}`);
      } else if (now + ps.D - clockRef(ps) >= r.everyMs) {
        if (triggerDemand(s, pid, now)) emit('refresh', `${r.name} on ${pname(s, pid)}`);
      }
    }
  }

  // 3. Start everything runnable, to a fixpoint (a start can cascade demand that enables more). Then
  // settle the throttle: a heartbeat can send a held request, which can enable more starts, so repeat.
  for (let round = 0; round < 16; round++) {
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
    if (!settleThrottle(s, now, completedPonds)) break;
  }

  return s;
}
