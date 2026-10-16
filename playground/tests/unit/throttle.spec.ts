import { test, expect } from '@playwright/test';
import { tick, refreshNow, drainLog, type OrchestrState } from '../../src/lib/orchestration';
import type { PondRunState, RippleRunState } from '../../src/lib/types';

// The throttle: checked on the topologies it was developed against. Under saturated demand (one-off Refreshes
// every second, a continuous Refresh, or one every 1 s with drifting ticks), the bottleneck runs as often as
// under a Wave, upstream settles at the bottleneck's rate, and the data is at least as fresh as a Wave's. Sparse
// Refreshes run every Pond once each. Until the bottleneck has started its first run nothing can be detected, so
// the startup runs unthrottled: up to one extra upstream run per Refresh for the length of the lead.
//
// The engine had a pull trigger, the Wave, until Refresh replaced it; WAVE holds what it measured on each
// topology (this simulation, 120 s), as the yardstick. Data age is therefore compared over the second half, as
// upstream run counts are (and as the Python port does).

const pondState = (): PondRunState => ({
  startF: 0, endF: 0, D: 0, targets: [], isKilled: false,
  isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [],
});
const rippleState = (): RippleRunState => ({
  startF: 0, endF: 0, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null,
  lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [],
});

// Ponds as [name, seconds (or a sequence of Ripples' seconds), sources].
function graph(spec: [string, number | number[], string[]][]): OrchestrState {
  const s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, refreshes: {} };
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
interface Recorded { runs: Record<string, number>; age: number }

// What a Wave measured on each topology, exact and with drifting ticks (see the top of the file).
const WAVE: Record<string, { wave: Recorded; waveDrift: Recorded }> = {"demo (sales pipeline)": {"wave": {"runs": {"transactions": 41, "products": 40, "sales": 38, "reports": 38}, "age": 10}, "waveDrift": {"runs": {"transactions": 44, "products": 43, "sales": 41, "reports": 41}, "age": 10.2378}}, "deep chain (fast stages, then a 5 s bottleneck)": {"wave": {"runs": {"I": 28, "P1": 27, "P2": 26, "P3": 25, "P4": 24, "B": 22, "O": 22}, "age": 31}, "waveDrift": {"runs": {"I": 30, "P1": 29, "P2": 28, "P3": 27, "P4": 26, "B": 24, "O": 24}, "age": 31.5025}}, "long lead (seven 1 s stages, then 3 s)": {"wave": {"runs": {"I": 45, "P1": 44, "P2": 43, "P3": 42, "P4": 41, "P5": 40, "P6": 39, "B": 37, "O": 37}, "age": 25}, "waveDrift": {"runs": {"I": 48, "P1": 47, "P2": 46, "P3": 45, "P4": 44, "P5": 43, "P6": 42, "B": 40, "O": 40}, "age": 25.5173}}, "decreasing chain (1 s \u2192 3 s \u2192 2 s \u2192 1 s)": {"wave": {"runs": {"A": 41, "B": 39, "C": 38, "D": 38}, "age": 9}, "waveDrift": {"runs": {"A": 44, "B": 42, "C": 41, "D": 41}, "age": 9.1988}}, "slow sibling Inlet": {"wave": {"runs": {"F": 20, "S": 19, "X": 19, "O": 19}, "age": 8}, "waveDrift": {"runs": {"F": 22, "S": 21, "X": 21, "O": 21}, "age": 8.2089}}, "diamond, bottleneck on one path": {"wave": {"runs": {"A": 25, "B": 23, "D": 24, "C": 23}, "age": 11}, "waveDrift": {"runs": {"A": 27, "B": 25, "D": 26, "C": 25}, "age": 11.1607}}, "diamond, long lead on the slow path": {"wave": {"runs": {"A": 81, "L1": 43, "L2": 42, "L3": 41, "L4": 40, "L5": 39, "B": 37, "D": 38, "C": 37}, "age": 21}, "waveDrift": {"runs": {"A": 87, "L1": 46, "L2": 45, "L3": 44, "L4": 43, "L5": 42, "B": 40, "D": 41, "C": 40}, "age": 21.277}}, "Inlet with Ripples in sequence (1 s \u2192 2 s \u2192 1.5 s)": {"wave": {"runs": {"I": 58, "O": 58}, "age": 6.5}, "waveDrift": {"runs": {"I": 62, "O": 61}, "age": 6.7288}}, "Outlet is the bottleneck (1 s \u2192 4 s)": {"wave": {"runs": {"I": 31, "O": 29}, "age": 8}, "waveDrift": {"runs": {"I": 33, "O": 32}, "age": 8.0872}}, "Outlet is the bottleneck behind a long lead (six 1 s stages, then 4 s)": {"wave": {"runs": {"I": 35, "P1": 34, "P2": 33, "P3": 32, "P4": 31, "P5": 30, "O": 28}, "age": 28}, "waveDrift": {"runs": {"I": 37, "P1": 36, "P2": 35, "P3": 34, "P4": 33, "P5": 32, "O": 30}, "age": 28.3933}}, "bottleneck inside a Pond (1 s \u2192 4 s \u2192 1 s)": {"wave": {"runs": {"I": 32, "P": 29, "O": 28}, "age": 14}, "waveDrift": {"runs": {"I": 34, "P": 31, "O": 31}, "age": 14.3099}}};

// Drive sim time in 100 ms ticks: one-off Refreshes at `outlet` every `every` seconds, a continuous Refresh
// (continuous), or a Refresh every 1 s (every1).
// `drift` advances time by 90-129 ms per tick, as the playground does (it ticks on real elapsed time), so
// events that coincide on an exact 100 ms grid land in different ticks.
function simulate(s: OrchestrState, outlet: string, seconds: number, opts: { every?: number; continuous?: boolean; every1?: boolean; drift?: boolean }): Run {
  if (opts.continuous) s.refreshes = { [outlet]: [{ name: 'continuous', everyMs: 0 }] };
  if (opts.every1) s.refreshes = { [outlet]: [{ name: 'every 1 s', everyMs: 1000 }] };
  let seed = 7;
  const drift = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) % 40) - 10;
  let now = 1_000_000;
  const ages: number[] = [];
  let lastEnd = 0;
  let half: Record<string, number> = {};
  for (let i = 0; i < seconds * 10; i++) {
    if (i === seconds * 5) half = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
    if (opts.every && i % Math.round(opts.every * 10) === 0) s = refreshNow(s, outlet, now);
    now += 100 + (opts.drift ? drift() : 0);
    s = tick(now, s);
    const end = s.pondStates[outlet].endF;
    if (end !== lastEnd) {
      lastEnd = end;
      if (i >= seconds * 5) ages.push((now - end) / 1000); // second half only
    }
  }
  drainLog();
  const runs = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
  const late = Object.fromEntries(Object.entries(runs).map(([k, v]) => [k, v - (half[k] ?? 0)])); // second half only
  return { runs, late, age: ages.reduce((a, b) => a + b, 0) / Math.max(1, ages.length) };
}

for (const [name, [make, outlet, inlet, bottleneck]] of Object.entries(TOPOLOGIES)) {
  test(`${name}: saturated demand runs at the bottleneck's pace, at least as fresh as a Wave`, () => {
    const wave = WAVE[name].wave;
    for (const opts of [{ every: 1 }, { continuous: true }, { every1: true, drift: true }]) {
      const throttled = simulate(make(), outlet, 120, opts);
      const ref = 'drift' in opts ? WAVE[name].waveDrift : wave;
      expect(throttled.runs[bottleneck]).toBe(ref.runs[bottleneck]);
      expect(throttled.runs[outlet]).toBe(ref.runs[outlet]);
      expect(throttled.late[inlet]).toBeLessThanOrEqual(throttled.late[bottleneck] + 1); // settled: one per bottleneck run
      expect(throttled.runs[inlet]).toBeLessThanOrEqual(wave.runs[inlet] + 14); // the startup, before detection, plus top-ups
      expect(throttled.age).toBeLessThanOrEqual(ref.age + 0.1);
    }
  });

  test(`${name}: sparse Refreshes run every Pond once each`, () => {
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

test("a Pond already throttled through another Refresh's Source still sends its first request", () => {
  // B is paced by its own continuous Refresh, so X (reading A and B) is throttled before it ever runs. X's one
  // Refresh must still reach A, which nothing else asks: held until X's first completion, it never would.
  const s = graph([['S', 1, []], ['A', 1, ['S']], ['B', 3, ['S']], ['X', 1, ['A', 'B']]]);
  s.refreshes = { B: [{ name: 'continuous', everyMs: 0 }] };
  let st = s;
  let now = 1_000_000;
  for (let i = 0; i < 300; i++) {
    if (i === 100) st = refreshNow(st, 'X', now);
    now += 100;
    st = tick(now, st);
  }
  expect(st.pondStates.X.runsCompleted).toBe(1);
  expect(st.pondStates.B.runsCompleted).toBeGreaterThan(5);
});
