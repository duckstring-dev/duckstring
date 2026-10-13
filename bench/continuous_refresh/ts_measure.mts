// A continuous refresh (Tide, bound 0) against a Wave under noisy run times, in the playground engine.
// Same scenarios and noise as py_measure.py. Run from this directory:
//   node --experimental-strip-types ts_measure.mts topologies.json 600 8 > results/ts.jsonl
import { tick, type OrchestrState } from '../../playground/src/lib/orchestration.ts';
import { readFileSync } from 'node:fs';

const TOPS = JSON.parse(readFileSync(process.argv[2], 'utf8')) as Record<string, [[string, number | number[], string[]][], string, string, string]>;
const seconds = Number(process.argv[3]), seeds = Number(process.argv[4]);

const fmix = (x: number) => { x >>>= 0; x ^= x >>> 16; x = Math.imul(x, 0x85ebca6b) >>> 0; x ^= x >>> 13; x = Math.imul(x, 0xc2b2ae35) >>> 0; x ^= x >>> 16; return x >>> 0; };
const fnv = (s: string) => { let h = 0x811c9dc5; for (const b of Buffer.from(s)) { h ^= b; h = Math.imul(h, 0x01000193) >>> 0; } return h >>> 0; };
function mult(seed: number, rid: string, k: number, sigma: number): number {
  if (sigma === 0) return 1;
  const inner = Number((BigInt(seed) * 0x9e3779b9n + BigInt(k)) & 0xffffffffn);
  const key = (fnv(rid) ^ fmix(inner)) >>> 0;
  const kk = fmix(key);
  const u1 = (fmix((kk ^ 0x68e31da4) >>> 0) + 1) / 4294967297;
  const u2 = (fmix((kk ^ 0xb5297a4d) >>> 0) + 1) / 4294967297;
  return Math.exp(sigma * Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2));
}

function build(name: string): OrchestrState {
  const s: OrchestrState = { ponds: {}, pondStates: {}, ripples: {}, rippleStates: {}, triggers: {} };
  const rs = () => ({ startF: 0, endF: 0, hasPull: false, targets: [], isRunning: false, runStartedAt: null, currentRunDurationMs: null, lastDurationMs: null, runsStarted: 0, runsCompleted: 0, completionTimes: [], durations: [] });
  const add = (id: string, pondId: string, nm: string, parents: string[], d: number) => { s.ripples[id] = { id, pondId, name: nm, parents, durationMs: d * 1000, variability: 0 }; s.rippleStates[id] = rs(); };
  for (const [pn, secs, sources] of TOPS[name][0]) {
    s.ponds[pn] = { id: pn, name: pn, sources };
    s.pondStates[pn] = { startF: 0, endF: 0, D: 0, hasReceivedPull: false, hasPull: false, pullLocal: false, targets: [], isKilled: false, isBlocked: false, runsStarted: 0, runsCompleted: 0, startTimesByF: {}, completionTimes: [], durations: [] } as any;
    (Array.isArray(secs) ? secs : [secs]).forEach((d, i) => add(`${pn}.r${i}`, pn, `r${i}`, i ? [`${pn}.r${i - 1}`] : [], d));
  }
  if (name === 'demo') { add('sales.r1', 'sales', 'tiers', [], 1); add('sales.r2', 'sales', 'join', ['sales.r0', 'sales.r1'], 3); }
  return s;
}

for (const name of Object.keys(TOPS)) {
  const [, outlet, inlet, bottleneck] = TOPS[name];
  for (const mode of ['wave', 'cont']) for (const sigma of [0, 0.25, 0.5, 1.0]) for (let seed = 0; seed < seeds; seed++) {
    let s = build(name);
    s.triggers = { [outlet]: mode === 'wave' ? { pondId: outlet, kind: 'wave' } : { pondId: outlet, kind: 'tide', stalenessMs: 0 } } as any;
    const seen: Record<string, number> = {};
    let now = 1_000_000, last = 0, workMs = 0;
    const ages: number[] = [];
    let half: Record<string, number> = {};
    for (let i = 0; i < seconds * 10; i++) {
      if (i === seconds * 5) half = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
      now += 100;
      s = tick(now, s);
      for (const [rid, st] of Object.entries(s.rippleStates)) {
        if (st.runsStarted !== (seen[rid] ?? 0)) {
          seen[rid] = st.runsStarted;
          const ms = Math.max(1, Math.round(s.ripples[rid].durationMs * mult(seed, rid, st.runsStarted, sigma)));
          st.currentRunDurationMs = ms;
          workMs += ms;
        }
      }
      const e = s.pondStates[outlet].endF;
      if (e !== last) { last = e; if (i >= seconds * 5) ages.push((now - e) / 1000); }
    }
    const runs = Object.fromEntries(Object.entries(s.pondStates).map(([k, v]) => [k, v.runsCompleted]));
    ages.sort((a, b) => a - b);
    console.log(JSON.stringify({ engine: 'ts', topo: name, mode, sigma, seed,
      outlet: runs[outlet] - half[outlet], bottleneck: runs[bottleneck] - half[bottleneck], inlet: runs[inlet] - half[inlet],
      total: Object.keys(runs).reduce((a, k) => a + runs[k] - (half[k] ?? 0), 0), work_s: workMs / 1000,
      age: ages.reduce((a, b) => a + b, 0) / Math.max(1, ages.length), age95: ages.length ? ages[Math.floor(0.95 * (ages.length - 1))] : null }));
  }
}
