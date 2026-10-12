import { test, expect } from '@playwright/test';
import { tick, pulsePond, drainLog, type OrchestrState } from '../../src/lib/orchestration';
import type { PondRunState, RippleRunState, TriggerKind } from '../../src/lib/types';

// Pulse release: the topologies it was first simulated on, checked against the properties that matter.
// Saturated demand runs upstream at about Wave's rate, sparse demand behaves exactly like Pulse, and the
// data is never staler than under Wave (fresher where fast stages sit above a bottleneck).

const pondState = (): PondRunState => ({
  startF: 0, endF: 0, D: 0, hasReceivedPull: false, hasPull: false, pullLocal: false, targets: [], isKilled: false,
  isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [],
});
const rippleState = (): RippleRunState => ({
  startF: 0, endF: 0, hasPull: false, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null,
  lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [],
});

// Ponds as [name, seconds, sources]: one Ripple each.
function graph(spec: [string, number, string[]][]): OrchestrState {
  const s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, triggers: {} };
  for (const [name, secs, sources] of spec) {
    s.ponds[name] = { id: name, name, sources };
    s.pondStates[name] = pondState();
    s.ripples[`${name}.r`] = { id: `${name}.r`, pondId: name, name: 'r', parents: [], durationMs: secs * 1000, variability: 0 };
    s.rippleStates[`${name}.r`] = rippleState();
  }
  return s;
}

const deep = () => graph([['I', 1, []], ['P1', 1, ['I']], ['P2', 1, ['P1']], ['P3', 1, ['P2']], ['P4', 1, ['P3']], ['B', 5, ['P4']], ['O', 1, ['B']]]);
const decreasing = () => graph([['A', 1, []], ['B', 3, ['A']], ['C', 2, ['B']], ['D', 1, ['C']]]);
const diamond = () => graph([['A', 1, []], ['B', 5, ['A']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]]);
const diamondLeadOnSlow = () =>
  graph([['A', 1, []], ['L1', 1, ['A']], ['L2', 1, ['L1']], ['L3', 1, ['L2']], ['L4', 1, ['L3']], ['L5', 1, ['L4']], ['B', 3, ['L5']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]]);
const longLead = () =>
  graph([['I', 1, []], ['P1', 1, ['I']], ['P2', 1, ['P1']], ['P3', 1, ['P2']], ['P4', 1, ['P3']], ['P5', 1, ['P4']], ['P6', 1, ['P5']], ['B', 3, ['P6']], ['O', 1, ['B']]]);
const sibling = () => graph([['F', 1, []], ['S', 6, []], ['X', 1, ['F', 'S']], ['O', 1, ['X']]]);

interface Run { runs: Record<string, number>; age: number }

// Drive `seconds` of sim time in 100 ms ticks: Pulses at `outlet` every `every` seconds, or a standing trigger.
function simulate(s: OrchestrState, outlet: string, seconds: number, opts: { every?: number; trigger?: TriggerKind; release: boolean }): Run {
  s.pulseRelease = opts.release;
  if (opts.trigger) s.triggers = { [outlet]: { pondId: outlet, kind: opts.trigger, stalenessMs: 0 } };
  let now = 1_000_000;
  const ages: number[] = [];
  let lastEnd = 0;
  for (let i = 0; i < seconds * 10; i++) {
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
  return { runs, age: ages.slice(1).reduce((a, b) => a + b, 0) / Math.max(1, ages.length - 1) };
}

const near = (a: number, b: number, tol: number) => expect(Math.abs(a - b)).toBeLessThanOrEqual(tol);

test('deep chain: saturated Pulses run upstream at the bottleneck rate, fresher than a Wave', () => {
  const pulse = simulate(deep(), 'O', 120, { every: 1, release: false });
  const release = simulate(deep(), 'O', 120, { every: 1, release: true });
  const wave = simulate(deep(), 'O', 120, { trigger: 'wave', release: false });
  expect(pulse.runs.I).toBeGreaterThan(100); // plain Pulse runs the Inlet on every demand
  near(release.runs.I, wave.runs.I, 3);
  expect(release.runs.O).toBe(wave.runs.O);
  expect(release.age).toBeLessThan(wave.age / 2); // the fast stages run back to back, not a cycle apart
});

test('decreasing chain: every stage settles at the 3 s bottleneck, as fresh as a Wave', () => {
  const release = simulate(decreasing(), 'D', 120, { every: 1, release: true });
  const wave = simulate(decreasing(), 'D', 120, { trigger: 'wave', release: false });
  near(release.runs.A, wave.runs.A, 2);
  for (const p of ['B', 'C', 'D']) expect(release.runs[p]).toBe(wave.runs[p]);
  expect(release.age).toBeLessThanOrEqual(wave.age + 0.1);
});

test('slow sibling: the fast Inlet is paced by the slow one', () => {
  const pulse = simulate(sibling(), 'O', 120, { every: 1, release: false });
  const release = simulate(sibling(), 'O', 120, { every: 1, release: true });
  const wave = simulate(sibling(), 'O', 120, { trigger: 'wave', release: false });
  expect(pulse.runs.F).toBeGreaterThan(100);
  near(release.runs.F, wave.runs.F, 2);
  expect(release.runs.O).toBe(wave.runs.O);
  expect(release.age).toBeLessThan(wave.age + 0.5);
});

test('diamond: the fast path is paced by the slow one', () => {
  const pulse = simulate(diamond(), 'C', 120, { every: 1, release: false });
  const release = simulate(diamond(), 'C', 120, { every: 1, release: true });
  const wave = simulate(diamond(), 'C', 120, { trigger: 'wave', release: false });
  expect(pulse.runs.D).toBeGreaterThan(100); // plain Pulse routes around the bottleneck
  near(release.runs.A, wave.runs.A, 2);
  near(release.runs.D, wave.runs.D, 2);
  expect(release.runs.C).toBe(wave.runs.C);
  expect(release.age).toBeLessThanOrEqual(wave.age + 0.1);
});

test('diamond with a long lead on the slow path keeps the bottleneck busy', () => {
  const release = simulate(diamondLeadOnSlow(), 'C', 120, { every: 1, release: true });
  const wave = simulate(diamondLeadOnSlow(), 'C', 120, { trigger: 'wave', release: false });
  expect(release.runs.B).toBe(wave.runs.B);
  expect(release.runs.D).toBeLessThan(wave.runs.D + 10);
  expect(release.age).toBeLessThan(wave.age / 1.5);
});

// An Inlet whose three Ripples run in sequence (1 s → 2 s → 1.5 s), feeding one 1 s Sink. Pipelined,
// it completes every 2 s; held back until all three are idle, it would complete every 4.5 s.
function pipelinedInlet(): OrchestrState {
  const s = graph([['I', 1, []], ['O', 1, ['I']]]);
  s.ripples['I.r2'] = { id: 'I.r2', pondId: 'I', name: 'r2', parents: ['I.r'], durationMs: 2000, variability: 0 };
  s.ripples['I.r3'] = { id: 'I.r3', pondId: 'I', name: 'r3', parents: ['I.r2'], durationMs: 1500, variability: 0 };
  s.rippleStates['I.r2'] = rippleState();
  s.rippleStates['I.r3'] = rippleState();
  return s;
}

test('a multi-Ripple Inlet still pipelines its Ripples', () => {
  const off = simulate(pipelinedInlet(), 'O', 60, { every: 1, release: false });
  const on = simulate(pipelinedInlet(), 'O', 60, { every: 1, release: true });
  const wave = simulate(pipelinedInlet(), 'O', 60, { trigger: 'wave', release: false });
  near(on.runs.O, off.runs.O, 2); // a run every 2 s, not one every 4.5 s
  expect(on.age).toBeLessThanOrEqual(wave.age + 0.1);
});

test('a long lead above the bottleneck keeps it busy', () => {
  const release = simulate(longLead(), 'O', 120, { every: 1, release: true });
  const wave = simulate(longLead(), 'O', 120, { trigger: 'wave', release: false });
  expect(release.runs.B).toBe(wave.runs.B);
  near(release.runs.I, wave.runs.I, 3);
  expect(release.age).toBeLessThan(wave.age / 1.5);
});

test('sparse Pulses are untouched', () => {
  const pulse = simulate(deep(), 'O', 120, { every: 60, release: false });
  const release = simulate(deep(), 'O', 120, { every: 60, release: true });
  expect(release).toEqual(pulse);
});

test('a zero-bound Tide with release behaves like a Wave', () => {
  for (const [make, outlet] of [[deep, 'O'], [decreasing, 'D'], [sibling, 'O'], [diamond, 'C']] as const) {
    const tide0 = simulate(make(), outlet, 120, { trigger: 'tide', release: true });
    const wave = simulate(make(), outlet, 120, { trigger: 'wave', release: false });
    expect(tide0.runs[outlet]).toBe(wave.runs[outlet]);
    const inlet = Object.keys(tide0.runs)[0];
    expect(tide0.runs[inlet]).toBeLessThan(wave.runs[inlet] * 1.25);
    expect(tide0.age).toBeLessThan(wave.age + 0.5);
  }
});

test('turning release off forwards anything held', () => {
  let s = deep();
  s.pulseRelease = true;
  let now = 1_000_000;
  for (let i = 0; i < 200; i++) {
    if (i % 10 === 0) s = pulsePond(s, 'O', now);
    now += 100;
    s = tick(now, s);
  }
  expect(Object.values(s.pondStates).some((p) => (p.release?.held ?? 0) > 0)).toBe(true);
  s.pulseRelease = false;
  s = tick(now + 100, s);
  expect(Object.values(s.pondStates).every((p) => p.release === undefined)).toBe(true);
  drainLog();
});
