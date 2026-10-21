import { tick, refreshNow, launchAlign, type OrchestrState } from '../../playground/src/lib/orchestration.ts';
type Spec = [string, number | number[], string[]][];
const ps = () => ({ startF: 0, endF: 0, D: 0, targets: [], isKilled: false, isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [] });
const rsx = () => ({ startF: 0, endF: 0, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null, lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [] });
function graph(spec: Spec, sigma: number): OrchestrState {
  const s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, refreshes: {} };
  for (const [name, secs, sources] of spec) { s.ponds[name] = { id: name, name, sources }; s.pondStates[name] = ps() as any;
    (Array.isArray(secs) ? secs : [secs]).forEach((d, i) => { const id = `${name}.r${i}`;
      s.ripples[id] = { id, pondId: name, name: `r${i}`, parents: i ? [`${name}.r${i - 1}`] : [], durationMs: d * 1000, variability: sigma }; s.rippleStates[id] = rsx() as any; }); }
  return s;
}
function demo(sigma: number, inlet: number, daily: number): OrchestrState {
  const s = graph([['transactions', inlet, []], ['products', inlet, []], ['sales', daily, ['transactions', 'products']], ['reports', 1, ['sales']]], sigma);
  s.ripples['sales.r1'] = { id: 'sales.r1', pondId: 'sales', name: 'tiers', parents: [], durationMs: 1000, variability: sigma };
  s.ripples['sales.r2'] = { id: 'sales.r2', pondId: 'sales', name: 'join', parents: ['sales.r0', 'sales.r1'], durationMs: 3000, variability: sigma };
  s.rippleStates['sales.r1'] = rsx() as any; s.rippleStates['sales.r2'] = rsx() as any; return s;
}
const chain = (spec: [string, number | number[]][], sg: number) => graph(spec.map(([n, d], i) => [n, d, i ? [spec[i - 1][0]] : []]), sg);
const T: Record<string, [(sg: number) => OrchestrState, string]> = {
  'demo (tour: 0.5/2/3)': [(sg) => demo(sg, 0.5, 2), 'reports'],
  'demo (old: 1,2 inlets)': [(sg) => { const s = demo(sg, 1, 2); s.ripples['products.r0'].durationMs = 2000; return s; }, 'reports'],
  'deep chain 5s': [(sg) => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['B', 5], ['O', 1]], sg), 'O'],
  'long lead 7x1 then 3': [(sg) => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['P5', 1], ['P6', 1], ['B', 3], ['O', 1]], sg), 'O'],
  'decreasing 1/3/2/1': [(sg) => chain([['A', 1], ['B', 3], ['C', 2], ['D', 1]], sg), 'D'],
  'slow sibling Inlet': [(sg) => graph([['F', 1, []], ['S', 6, []], ['X', 1, ['F', 'S']], ['O', 1, ['X']]], sg), 'O'],
  'diamond bottleneck': [(sg) => graph([['A', 1, []], ['B', 5, ['A']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]], sg), 'C'],
  'diamond long lead': [(sg) => graph([['A', 1, []], ['L1', 1, ['A']], ['L2', 1, ['L1']], ['L3', 1, ['L2']], ['L4', 1, ['L3']], ['L5', 1, ['L4']], ['B', 3, ['L5']], ['D', 1, ['A']], ['C', 1, ['B', 'D']]], sg), 'C'],
  'inlet seq 1/2/1.5': [(sg) => chain([['I', [1, 2, 1.5]], ['O', 1]], sg), 'O'],
  'outlet bottleneck 1/4': [(sg) => chain([['I', 1], ['O', 4]], sg), 'O'],
  'outlet bn long lead': [(sg) => chain([['I', 1], ['P1', 1], ['P2', 1], ['P3', 1], ['P4', 1], ['P5', 1], ['O', 4]], sg), 'O'],
  'bottleneck in pond 1/4/1': [(sg) => chain([['I', 1], ['P', [1, 4, 1]], ['O', 1]], sg), 'O'],
  'lead 2.9 vs 3 (knife)': [(sg) => chain([['I', 0.9], ['P', 2], ['B', 3], ['O', 1]], sg), 'O'],
  'lead 3.1 vs 3 (knife)': [(sg) => chain([['I', 1.1], ['P', 2], ['B', 3], ['O', 1]], sg), 'O'],
};
let seed = 1;
Math.random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
function run(make: (sg: number) => OrchestrState, outlet: string, mode: string, sigma: number, frame: number, on: boolean, sd = 12345) {
  launchAlign.on = on; seed = sd;
  let s = make(sigma); const secs = 240; const t0 = 1_000_000; let now = t0;
  if (mode === 'cont') s.refreshes = { [outlet]: [{ name: 'r', everyMs: 0 }] };
  if (mode === 'every1d') s.refreshes = { [outlet]: [{ name: 'r', everyMs: 1000 }] };
  let nextOneOff = t0;
  while (now < t0 + secs * 1000) {
    if (mode === 'oneoff' && now >= nextOneOff) { s = refreshNow(s, outlet, now); nextOneOff += 1000; }
    const target = now + (frame === 100 ? 100 + (mode === 'every1d' ? ((seed % 40) - 10) : 0) : frame * (0.9 + 0.2 * Math.random()));
    while (target - now > 100) { now += 100; s = tick(now, s); }
    now = target; s = tick(now, s);
  }
  const p = s.pondStates[outlet]; const half = t0 + secs * 500;
  const C = p.completionTimes, A = p.ages ?? [];
  let area = 0, span = 0, n = 0;
  for (let i = 1; i < C.length; i++) { if (C[i - 1] < half) continue; const g = C[i] - C[i - 1]; area += (A[i - 1] + g / 2) * g; span += g; n++; }
  return { runs: n, age: span ? area / span / 1000 : NaN };
}
const rows: string[] = []; const sel = process.env.SPREAD; if (sel != null) launchAlign.spread = Number(sel); if (process.env.WIN) launchAlign.window = Number(process.env.WIN); if (process.env.GATE) launchAlign.gate = Number(process.env.GATE); let worst = { runs: 0, age: 0, name: '' };
for (const frame of [100, 1000]) for (const mode of ['cont', 'oneoff', 'every1d']) for (const sigma of [0, 0.2, 0.5]) {
  if (frame === 1000 && mode === 'every1d') continue;
  for (const [name, [make, outlet]] of Object.entries(T)) {
    const seeds = sigma ? [11, 22, 33, 44, 55, 66] : [1];
    const avg = (on: boolean) => { const r = seeds.map((sd) => run(make, outlet, mode, sigma, frame, on, sd)); return { runs: r.reduce((a, b) => a + b.runs, 0) / r.length, age: r.reduce((a, b) => a + b.age, 0) / r.length }; };
    const off = avg(false), on = avg(true);
    const dr = (on.runs - off.runs) / Math.max(1, off.runs), da = on.age - off.age;
    if (dr < worst.runs) worst = { ...worst, runs: dr, name: `${name} ${mode} σ${sigma} f${frame}` };
    rows.push(`${String(frame).padStart(4)} ${mode.padEnd(7)} σ${sigma.toFixed(1)} ${name.padEnd(24)} runs ${off.runs.toFixed(1).padStart(5)}→${on.runs.toFixed(1).padStart(5)} (${(dr * 100).toFixed(0).padStart(3)}%)  age ${off.age.toFixed(2).padStart(6)}→${on.age.toFixed(2).padStart(6)} (${da >= 0 ? '+' : ''}${da.toFixed(2)})`);
  }
}
console.log(rows.join('\n'));
