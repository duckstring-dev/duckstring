import { test, expect } from '@playwright/test';
import { tick, pulsePond, drainLog, type OrchestrState } from '../../src/lib/orchestration';
import type { PondRunState, RippleRunState } from '../../src/lib/types';

// Pulse throttle: checked on the topologies it was developed against. Under saturated demand, the
// bottleneck runs as often as under a Wave, upstream settles at the bottleneck's rate, and the data is at
// least as fresh as a Wave's. Sparse demand runs every Pond once per Pulse. Until the bottleneck has
// started its first run nothing can be detected, so the startup runs as plain Pulse: up to one extra
// upstream run per Pulse for the length of the lead.

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
// The playground's demo: two Inlets feed `sales`, whose `join` (3 s) waits on `daily` (2 s) and `tiers` (1 s).
function demo(): OrchestrState {
  const s = graph([['transactions', 1, []], ['products', 2, []], ['sales', 2, ['transactions', 'products']], ['reports', 1, ['sales']]]);
  s.ripples['sales.r1'] = { id: 'sales.r1', pondId: 'sales', name: 'tiers', parents: [], durationMs: 1000, variability: 0 };
  s.ripples['sales.r2'] = { id: 'sales.r2', pondId: 'sales', name: 'join', parents: ['sales.r0', 'sales.r1'], durationMs: 3000, variability: 0 };
  s.rippleStates['sales.r1'] = rippleState();
  s.rippleStates['sales.r2'] = rippleState();
  return s;
}
const chain = (spec: [string, number | number[]][]) => graph(spec.map(([n, d], i) => [n, d, i ? [spec[i - 1][0]] : []]));

// [build, Outlet, an Inlet whose rate matters, the bottleneck Pond]
const TOPOLOGIES: Record<string, [() => OrchestrState, string, string, string]> = {
  'demo (sales pipeline)': [demo, 'reports', 'transactions', 'sales'],
  'deep chain (fast stages, then a 5 s bottleneck)': [() => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['B', 5], ['O', 1]]), 'O', 'I', 'B'],
  'long lead (seven 1 s stages, then 3 s)': [() => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['P5', 1], ['P6', 1], ['B', 3], ['O', 1]]), 'O', 'I', 'B'],
  'decreasing chain (1 s → 3 s → 2 s → 1 s)': [() => chain([['A', 1], ['B', 3], ['C', 2], ['D', 1]]), 'D', 'A', 'B'],
  'slow sibling Inlet': [() => graph([['F', 1, []], ['S', 6, []], ['X', 1, ['F', 'S']], ['O', 1, ['X']]]), 'O', 'F', 'S'],
  'diamond, bottleneck on one path': [() => graph([['A', 1, []], ['B', 5, ['A']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]]), 'C', 'A', 'B'],
  'diamond, long lead on the slow path': [() => graph([['A', 1, []], ['L1', 1, ['A']], ['L2', 1, ['L1']], ['L3', 1, ['L2']], ['L4', 1, ['L3']], ['L5', 1, ['L4']], ['B', 3, ['L5']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]]), 'C', 'D', 'B'],
  'Inlet with Ripples in sequence (1 s → 2 s → 1.5 s)': [() => chain([['I', [1, 2, 1.5]], ['O', 1]]), 'O', 'I', 'I'],
  'Outlet is the bottleneck (1 s → 4 s)': [() => chain([['I', 1], ['O', 4]]), 'O', 'I', 'O'],
  'Outlet is the bottleneck behind a long lead (six 1 s stages, then 4 s)': [() => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['P5', 1], ['O', 4]]), 'O', 'I', 'O'],
  'bottleneck inside a Pond (1 s → 4 s → 1 s)': [() => chain([['I', 1], ['P', [1, 4, 1]], ['O', 1]]), 'O', 'I', 'P'],
};

interface Run { runs: Record<string, number>; late: Record<string, number>; age: number }

// Drive sim time in 100 ms ticks: Pulses at `outlet` every `every` seconds, a 0 s Tide, or a Wave.
// `drift` advances time by 90-129 ms per tick, as the playground does (it ticks on real elapsed time), so
// events that coincide on an exact 100 ms grid land in different ticks.
function simulate(s: OrchestrState, outlet: string, seconds: number, opts: { every?: number; tide0?: boolean; tide1?: boolean; wave?: boolean; drift?: boolean }): Run {
  if (opts.wave) s.triggers = { [outlet]: { pondId: outlet, kind: 'wave' } };
  if (opts.tide0) s.triggers = { [outlet]: { pondId: outlet, kind: 'tide', stalenessMs: 0 } };
  if (opts.tide1) s.triggers = { [outlet]: { pondId: outlet, kind: 'tide', stalenessMs: 1000 } };
  let seed = 7;
  const drift = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) % 40) - 10;
  let now = 1_000_000;
  const ages: number[] = [];
  let lastEnd = 0;
  let half: Record<string, number> = {};
  for (let i = 0; i < seconds * 10; i++) {
    if (i === seconds * 5) half = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
    if (opts.every && i % Math.round(opts.every * 10) === 0) s = pulsePond(s, outlet, now);
    now += 100 + (opts.drift ? drift() : 0);
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
    for (const opts of [{ every: 1 }, { tide0: true }, { tide1: true, drift: true }]) {
      const throttled = simulate(make(), outlet, 120, opts);
      const ref = 'drift' in opts ? simulate(make(), outlet, 120, { wave: true, drift: true }) : wave;
      expect(throttled.runs[bottleneck]).toBe(ref.runs[bottleneck]);
      expect(throttled.runs[outlet]).toBe(ref.runs[outlet]);
      expect(throttled.late[inlet]).toBeLessThanOrEqual(throttled.late[bottleneck] + 1); // settled: one per bottleneck run
      expect(throttled.runs[inlet]).toBeLessThanOrEqual(wave.runs[inlet] + 14); // the startup, before detection, plus top-ups
      expect(throttled.age).toBeLessThanOrEqual(ref.age + 0.1);
    }
  });

  test(`${name}: sparse Pulses run every Pond once per Pulse`, () => {
    const sparse = simulate(make(), outlet, 120, { every: 60 });
    for (const [pond, runs] of Object.entries(sparse.runs)) expect(runs, pond).toBe(2);
  });
}

test('a slow optional Source neither throttles its Sink nor is asked again once it has run', () => {
  // `C` requires the 1 s `A` and takes the 4 s `B` optionally.
  const s = graph([['A', 1, []], ['B', 4, []], ['C', 1, ['A', 'B']]]);
  s.ponds.C.optionalSources = ['B'];
  const r = simulate(s, 'C', 120, { every: 1 });
  expect(r.runs.B).toBe(1); // asked once, to build its first output; never again
  expect(r.runs.C).toBeGreaterThan(50); // paced by A (and its own 1 s run), never by B
});

test('a Pond with only optional Sources is throttled only when all of them are', () => {
  const s = graph([['A', 1, []], ['B', 4, []], ['C', 1, ['A', 'B']]]);
  s.ponds.C.optionalSources = ['A', 'B'];
  const r = simulate(s, 'C', 120, { every: 1 });
  expect(r.runs.A).toBeGreaterThan(50); // B is slow, A isn't, so C isn't throttled and A keeps up
  expect(r.runs.C).toBeGreaterThan(50);
});
