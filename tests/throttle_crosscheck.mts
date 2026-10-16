// Runs one scenario through the playground's TypeScript engine, for tests/test_engine.py's cross-check
// against the Python port. The simulation loop is playground/tests/unit/throttle.spec.ts's `simulate`.
//
//   node --experimental-strip-types tests/throttle_crosscheck.mts '<scenario JSON>'
//
// Scenario: {"spec": [[pond, seconds | [seconds, ...], [sources]], ...], "outlet": pond, "seconds": n,
// "every"?: s, "standing"?: s, "drift"?: true, "standings"?: {pond: s}, "schedules"?: {pond: [start s, every s]}}.
// `every` sends a one-off Refresh to the Outlet that often; `standing` is a standing Refresh every that many seconds
// (0 = continuous) on it, `standings` adds them on further Ponds (several Outlets with their own cadences), and `schedules`
// adds scheduled Refreshes (on the grid start + k * every, counted from the simulation's start). Prints {"runs": {pond: completed Pond Runs},
// "completions": {pond: [ms after the start, ...]}}.
import { refreshNow, tick, type OrchestrState } from '../playground/src/lib/orchestration.ts';

type Spec = [string, number | number[], string[]][];
const sc = JSON.parse(process.argv[2]) as {
  spec: Spec; outlet: string; seconds: number; every?: number; standing?: number; drift?: boolean;
  standings?: Record<string, number>; schedules?: Record<string, [number, number]>;
};

let s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, refreshes: {} };
for (const [name, secs, sources] of sc.spec) {
  s.ponds[name] = { id: name, name, sources };
  s.pondStates[name] = {
    startF: 0, endF: 0, D: 0, targets: [], isKilled: false,
    isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [],
  };
  (Array.isArray(secs) ? secs : [secs]).forEach((d, i) => {
    const id = `${name}.r${i}`;
    s.ripples[id] = { id, pondId: name, name: `r${i}`, parents: i ? [`${name}.r${i - 1}`] : [], durationMs: d * 1000, variability: 0 };
    s.rippleStates[id] = {
      startF: 0, endF: 0, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null,
      lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [],
    };
  });
}
if (sc.standing != null) s.refreshes = { [sc.outlet]: [{ name: 'refresh', everyMs: sc.standing * 1000 }] };
for (const [pond, secs] of Object.entries(sc.standings ?? {})) s.refreshes[pond] = [{ name: 'refresh', everyMs: secs * 1000 }];
const start = 1_000_000;
for (const [pond, [at, every]] of Object.entries(sc.schedules ?? {})) {
  s.refreshes[pond] = [...(s.refreshes[pond] ?? []), { name: 'scheduled', everyMs: every * 1000, startAnchor: start + at * 1000 }];
}

let seed = 7;
const drift = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) % 40) - 10;
let now = start;
for (let i = 0; i < sc.seconds * 10; i++) {
  if (sc.every && i % Math.round(sc.every * 10) === 0) s = refreshNow(s, sc.outlet, now);
  now += 100 + (sc.drift ? drift() : 0);
  s = tick(now, s);
}
const runs = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
const completions = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.completionTimes.map((t) => t - start)]));
console.log(JSON.stringify({ runs, completions }));
