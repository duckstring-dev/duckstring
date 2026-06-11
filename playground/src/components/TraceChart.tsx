'use client';

import { THEME_PULL, THEME_RUNNING } from '@/lib/store';

// Run-cadence + run-duration trace. Two lines share one y-axis so the run duration can be
// read against the cadence (≈ the bottleneck): well below it ⇒ this node isn't the bottleneck;
// close to it ⇒ it is. The y-axis is clipped near the 75th percentile (plus a margin
// proportional to the mean) so aberrant gaps — e.g. after a sleep — don't squash the rest.
const INTERVAL_COLOR = THEME_PULL; // run cadence, tied to Wave/pull (≈ bottleneck under a Wave)
const DURATION_COLOR = THEME_RUNNING; // run duration, tied to the running state

function quantile(sortedAsc: number[], q: number): number {
  if (sortedAsc.length === 0) return 0;
  const i = Math.floor(q * (sortedAsc.length - 1));
  return sortedAsc[i];
}

export function TraceChart({ times, durations }: { times: number[]; durations: number[] }) {
  const W = 256;
  const H = 100;
  const padL = 6;
  const padR = 46;
  const padT = 10;
  const padB = 14;

  // Intervals (s) between consecutive completions; run durations (s) per completion.
  const intervals: number[] = [];
  for (let i = 1; i < times.length; i++) intervals.push((times[i] - times[i - 1]) / 1000);
  const durs = durations.map((d) => d / 1000);

  const header = (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#a1a1aa', marginBottom: 4 }}>
      <span style={{ color: INTERVAL_COLOR }}>● interval</span>
      <span style={{ color: DURATION_COLOR }}>● run dur</span>
      <span style={{ color: '#71717a' }}>(s)</span>
    </div>
  );

  if (intervals.length === 0 && durs.length < 2) {
    return (
      <div>
        {header}
        <div style={{ fontSize: 11, color: '#52525b', padding: '28px 0', textAlign: 'center' }}>Needs 2+ completed runs.</div>
      </div>
    );
  }

  // Clip the y-axis: 75th percentile of all plotted values + margin ∝ mean.
  const all = [...intervals, ...durs];
  const sorted = [...all].sort((a, b) => a - b);
  const mean = all.reduce((a, b) => a + b, 0) / all.length;
  const yMax = Math.max(quantile(sorted, 0.9) + 0.4 * mean, 0.01);

  // Shared x-index by completion number (durations has one point per completion).
  const n = Math.max(durs.length, intervals.length + 1);
  const x = (i: number) => (n <= 1 ? padL : padL + (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => padT + (1 - Math.min(v, yMax) / yMax) * (H - padT - padB);

  const line = (vals: number[], color: string) => {
    if (vals.length === 0) return null;
    const pts = vals.map((v, i) => `${x(i)},${y(v)}`).join(' ');
    return (
      <>
        {vals.length > 1 && <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} />}
        {vals.map((v, i) => (
          <circle key={i} cx={x(i)} cy={y(v)} r={1.8} fill={color} />
        ))}
      </>
    );
  };

  const meanLast3 = (vals: number[]) => {
    const last3 = vals.slice(-3);
    return last3.length ? last3.reduce((a, b) => a + b, 0) / last3.length : null;
  };
  const intMean = meanLast3(intervals);
  const durMean = meanLast3(durs);

  const meanLine = (m: number | null, color: string) =>
    m == null ? null : (
      <>
        <line x1={padL} y1={y(m)} x2={W - padR} y2={y(m)} stroke={color} strokeWidth={1} strokeDasharray="4 3" />
        <text x={W - padR + 4} y={y(m) + 3} fontSize={10} fill={color} fontWeight={700}>
          {m.toFixed(1)}s
        </text>
      </>
    );

  return (
    <div>
      {header}
      <svg width={W} height={H} style={{ display: 'block' }}>
        <line x1={padL} y1={H - padB} x2={W - padR} y2={H - padB} stroke="#27272a" strokeWidth={1} />
        {meanLine(intMean, INTERVAL_COLOR)}
        {meanLine(durMean, DURATION_COLOR)}
        {line(durs, DURATION_COLOR)}
        {line(intervals, INTERVAL_COLOR)}
      </svg>
    </div>
  );
}
