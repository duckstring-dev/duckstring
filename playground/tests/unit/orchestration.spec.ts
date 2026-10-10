import { test, expect } from '@playwright/test';
import { tick, drainLog, type OrchestrState } from '../../src/lib/orchestration';
import type { PondRunState, Ripple, RippleRunState } from '../../src/lib/types';

const pondState = (): PondRunState => ({
  startF: 0, endF: 0, D: 0, hasReceivedPull: false, hasPull: false, pullLocal: false, targets: [], isKilled: false,
  isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [],
});
const rippleState = (): RippleRunState => ({
  startF: 0, endF: 0, hasPull: false, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null,
  lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [],
});
const ripple = (id: string, pondId: string, durationMs: number, parents: string[] = [], variability = 0): Ripple => ({
  id, pondId, name: id, parents, durationMs, variability,
});

// The example graph plus a second output on `sales`, under Tides of 5 s and 7 s, with noise on the
// bottleneck. A slow `join_lines` lets a newer `sales` Run absorb an older one still in flight.
function twoTides(): OrchestrState {
  return {
    ponds: {
      tx: { id: 'tx', name: 'transactions', sources: [] },
      prod: { id: 'prod', name: 'products', sources: [] },
      sales: { id: 'sales', name: 'sales', sources: ['tx', 'prod'] },
      reports: { id: 'reports', name: 'reports', sources: ['sales'] },
      forecast: { id: 'forecast', name: 'forecast', sources: ['sales'] },
    },
    pondStates: { tx: pondState(), prod: pondState(), sales: pondState(), reports: pondState(), forecast: pondState() },
    ripples: {
      txi: ripple('txi', 'tx', 1000),
      pi: ripple('pi', 'prod', 2000),
      ds: ripple('ds', 'sales', 2000),
      pt: ripple('pt', 'sales', 1000),
      jl: ripple('jl', 'sales', 3000, ['ds', 'pt'], 0.2),
      ms: ripple('ms', 'reports', 1000),
      fr: ripple('fr', 'forecast', 1000),
    },
    rippleStates: Object.fromEntries(['txi', 'pi', 'ds', 'pt', 'jl', 'ms', 'fr'].map((id) => [id, rippleState()])),
    triggers: {
      reports: { pondId: 'reports', kind: 'tide', stalenessMs: 5000 },
      forecast: { pondId: 'forecast', kind: 'tide', stalenessMs: 7000 },
    },
  };
}

// A seeded Math.random, so the noise (and the test) is repeatable.
function seeded(seed: number): () => number {
  return () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 2 ** 32;
  };
}

test('Pond Run durations stay bounded when Runs are absorbed', () => {
  const random = Math.random;
  Math.random = seeded(42);
  let s = twoTides();
  let now = 1_000_000;
  for (let i = 0; i < 3000; i++) {
    now += 100;
    s = tick(now, s);
  }
  drainLog();
  Math.random = random;
  const sales = s.pondStates.sales;
  // Absorbed Runs: more starts than completions beyond the two in flight. Pairing the Nth completion
  // with the Nth start drifted from the first one on, so durations grew with every absorption.
  expect(sales.runsStarted - sales.runsCompleted).toBeGreaterThan(2);
  // A `sales` Run takes about 5 s end to end, plus up to one wait for a noisy `join_lines`.
  expect(Math.max(...sales.durations.slice(-20))).toBeLessThanOrEqual(10000);
  expect(Object.keys(sales.startTimesByF).length).toBeLessThanOrEqual(3);

});
