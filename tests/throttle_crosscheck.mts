// Runs one scenario through the playground's TypeScript engine, for tests/test_engine.py's cross-check
// against the Python port. The simulation loop is playground/tests/unit/throttle.spec.ts's `simulate`.
//
//   node --experimental-strip-types tests/throttle_crosscheck.mts '<scenario JSON>'
//
// Scenario: {"spec": [[pond, seconds | [seconds, ...], [sources]], ...], "outlet": pond, "seconds": n,
// "every"?: s, "tide"?: s, "wave"?: true, "drift"?: true}. Prints {"runs": {pond: completed Pond Runs},
// "completions": {pond: [ms after the start, ...]}}.
import { pulsePond, tick, type OrchestrState } from '../playground/src/lib/orchestration.ts';

type Spec = [string, number | number[], string[]][];
const sc = JSON.parse(process.argv[2]) as {
  spec: Spec; outlet: string; seconds: number; every?: number; tide?: number; wave?: boolean; drift?: boolean;
};

let s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, triggers: {} };
for (const [name, secs, sources] of sc.spec) {
  s.ponds[name] = { id: name, name, sources };
  s.pondStates[name] = {
    startF: 0, endF: 0, D: 0, hasReceivedPull: false, hasPull: false, pullLocal: false, targets: [], isKilled: false,
    isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [],
  };
  (Array.isArray(secs) ? secs : [secs]).forEach((d, i) => {
    const id = `${name}.r${i}`;
    s.ripples[id] = { id, pondId: name, name: `r${i}`, parents: i ? [`${name}.r${i - 1}`] : [], durationMs: d * 1000, variability: 0 };
    s.rippleStates[id] = {
      startF: 0, endF: 0, hasPull: false, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null,
      lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [],
    };
  });
}
if (sc.wave) s.triggers = { [sc.outlet]: { pondId: sc.outlet, kind: 'wave' } };
if (sc.tide != null) s.triggers = { [sc.outlet]: { pondId: sc.outlet, kind: 'tide', stalenessMs: sc.tide * 1000 } };

let seed = 7;
const drift = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) % 40) - 10;
const start = 1_000_000;
let now = start;
for (let i = 0; i < sc.seconds * 10; i++) {
  if (sc.every && i % Math.round(sc.every * 10) === 0) s = pulsePond(s, sc.outlet, now);
  now += 100 + (sc.drift ? drift() : 0);
  s = tick(now, s);
}
const runs = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
const completions = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.completionTimes.map((t) => t - start)]));
console.log(JSON.stringify({ runs, completions }));
