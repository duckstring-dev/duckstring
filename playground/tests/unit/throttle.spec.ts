import { test, expect } from '@playwright/test';
import { tick, pulsePond, drainLog, type OrchestrState } from '../../src/lib/orchestration';
import type { PondRunState, RippleRunState } from '../../src/lib/types';

// Pulse throttle: checked on the topologies it was developed against. Under saturated demand, the
// bottleneck runs as often as under a Wave, upstream settles at the bottleneck's rate, and the data is at
// least as fresh as a Wave's. Sparse demand behaves exactly like Pulse. Until the bottleneck has started
// its first run nothing can be detected, so the startup runs as plain Pulse: up to one extra upstream
// run per Pulse for the length of the lead.

const pondState = (): PondRunState => ({
  startF: 0, endF: 0, D: 0, hasReceivedPull: false, hasPull: false, pullLocal: false, targets: [], isKilled: false,
  isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [],
});
const rippleState = (): RippleRunState => ({
  startF: 0, endF: 0, hasPull: false, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null,
  lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [],
});

// Ponds as [name, seconds (or a sequence of Ripples' seconds), sources].
function graph(spec: [string, number | number[], string[]][]): OrchestrState {
  const s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, triggers: {} };
  for (const [name, secs, sources] of spec) {
    s.ponds[name] = { id: name, name, sources };
    s.pondStates[name] = pondState();
    (Array.isArray(secs) ? secs : [secs]).forEach((d, i) => {
      const id = `${name}.r${i}`;
      s.ripples[id] = { id, pondId: name, name: `r${i}`, parents: i ? [`${name}.r${i - 1}`] : [], durationMs: d * 1000, variability: 0 };
      s.rippleStates[id] = rippleState();
    });
  }
  return s;
}
const chain = (spec: [string, number | number[]][]) => graph(spec.map(([n, d], i) => [n, d, i ? [spec[i - 1][0]] : []]));

// [build, Outlet, an Inlet whose rate matters, the bottleneck Pond]
const TOPOLOGIES: Record<string, [() => OrchestrState, string, string, string]> = {
  'deep chain (fast stages, then a 5 s bottleneck)': [() => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['B', 5], ['O', 1]]), 'O', 'I', 'B'],
  'long lead (seven 1 s stages, then 3 s)': [() => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['P5', 1], ['P6', 1], ['B', 3], ['O', 1]]), 'O', 'I', 'B'],
  'decreasing chain (1 s → 3 s → 2 s → 1 s)': [() => chain([['A', 1], ['B', 3], ['C', 2], ['D', 1]]), 'D', 'A', 'B'],
  'slow sibling Inlet': [() => graph([['F', 1, []], ['S', 6, []], ['X', 1, ['F', 'S']], ['O', 1, ['X']]]), 'O', 'F', 'S'],
  'diamond, bottleneck on one path': [() => graph([['A', 1, []], ['B', 5, ['A']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]]), 'C', 'A', 'B'],
  'diamond, long lead on the slow path': [() => graph([['A', 1, []], ['L1', 1, ['A']], ['L2', 1, ['L1']], ['L3', 1, ['L2']], ['L4', 1, ['L3']], ['L5', 1, ['L4']], ['B', 3, ['L5']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]]), 'C', 'D', 'B'],
  'Inlet with Ripples in sequence (1 s → 2 s → 1.5 s)': [() => chain([['I', [1, 2, 1.5]], ['O', 1]]), 'O', 'I', 'I'],
  'bottleneck inside a Pond (1 s → 4 s → 1 s)': [() => chain([['I', 1], ['P', [1, 4, 1]], ['O', 1]]), 'O', 'I', 'P'],
};

interface Run { runs: Record<string, number>; late: Record<string, number>; age: number }

// Drive sim time in 100 ms ticks: Pulses at `outlet` every `every` seconds, a 0 s Tide, or a Wave.
function simulate(s: OrchestrState, outlet: string, seconds: number, opts: { every?: number; tide0?: boolean; wave?: boolean; throttle?: boolean }): Run {
  s.pulseThrottle = !!opts.throttle;
  if (opts.wave) s.triggers = { [outlet]: { pondId: outlet, kind: 'wave' } };
  if (opts.tide0) s.triggers = { [outlet]: { pondId: outlet, kind: 'tide', stalenessMs: 0 } };
  let now = 1_000_000;
  const ages: number[] = [];
  let lastEnd = 0;
  let half: Record<string, number> = {};
  for (let i = 0; i < seconds * 10; i++) {
    if (i === seconds * 5) half = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
    if (opts.every && i % Math.round(opts.every * 10) === 0) s = pulsePond(s, outlet, now);
    now += 100;
    s = tick(now, s);
    const end = s.pondStates[outlet].endF;
    if (end !== lastEnd) {
      lastEnd = end;
      ages.push((now - end) / 1000);
    }
  }
  drainLog();
  const runs = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
  const late = Object.fromEntries(Object.entries(runs).map(([k, v]) => [k, v - (half[k] ?? 0)])); // second half only
  return { runs, late, age: ages.slice(1).reduce((a, b) => a + b, 0) / Math.max(1, ages.length - 1) };
}

for (const [name, [make, outlet, inlet, bottleneck]] of Object.entries(TOPOLOGIES)) {
  test(`${name}: saturated demand runs at the bottleneck's pace, at least as fresh as a Wave`, () => {
    const wave = simulate(make(), outlet, 120, { wave: true });
    for (const opts of [{ every: 1 }, { tide0: true }]) {
      const throttled = simulate(make(), outlet, 120, { ...opts, throttle: true });
      expect(throttled.runs[bottleneck]).toBe(wave.runs[bottleneck]);
      expect(throttled.runs[outlet]).toBe(wave.runs[outlet]);
      expect(throttled.late[inlet]).toBeLessThanOrEqual(throttled.late[bottleneck] + 1); // settled: one per bottleneck run
      expect(throttled.runs[inlet]).toBeLessThanOrEqual(wave.runs[inlet] + 10); // the startup, before detection
      expect(throttled.age).toBeLessThanOrEqual(wave.age + 0.1);
    }
  });

  test(`${name}: sparse Pulses are untouched`, () => {
    expect(simulate(make(), outlet, 120, { every: 60, throttle: true })).toEqual(simulate(make(), outlet, 120, { every: 60 }));
  });
}

test('plain Pulse over-runs upstream; the throttle does not', () => {
  const [make] = TOPOLOGIES['diamond, bottleneck on one path'];
  expect(simulate(make(), 'C', 120, { every: 1 }).runs.D).toBeGreaterThan(100);
  expect(simulate(make(), 'C', 120, { every: 1, throttle: true }).runs.D).toBeLessThan(30);
});

test('turning the throttle off sends any queued Pulse', () => {
  let s = TOPOLOGIES['deep chain (fast stages, then a 5 s bottleneck)'][0]();
  s.pulseThrottle = true;
  let now = 1_000_000;
  for (let i = 0; i < 200; i++) {
    if (i % 10 === 0) s = pulsePond(s, 'O', now);
    now += 100;
    s = tick(now, s);
  }
  expect(s.pondStates.O.throttle?.queued).toBe(true);
  s.pulseThrottle = false;
  s = tick(now + 100, s);
  expect(s.pondStates.O.throttle?.queued).toBe(false);
  drainLog();
});
