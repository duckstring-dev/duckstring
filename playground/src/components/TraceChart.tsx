'use client';

import { THEME_AMBER, THEME_DEMAND, THEME_RUNNING } from '@/lib/store';

// Run-cadence + run-duration trace. Two lines share one y-axis so the run duration can be
// read against the cadence (≈ the bottleneck): well below it ⇒ this node isn't the bottleneck;
// close to it ⇒ it is. The y-axis is clipped near the 75th percentile (plus a margin
// proportional to the mean) so aberrant gaps — e.g. after a sleep — don't squash the rest.
// Data age is a sawtooth: at each completion it drops to the age of the data just published, then grows with the
// time until the next one, so its peak is that age plus the interval. Its dashed line is the time-averaged age.
const INTERVAL_COLOR = THEME_AMBER; // run cadence (the bottleneck's pace under saturation)
const DURATION_COLOR = THEME_RUNNING; // run duration, tied to the running state
const AGE_COLOR = THEME_DEMAND; // data age (Ponds only), tied to demand/freshness

function quantile(sortedAsc: number[], q: number): number {
  if (sortedAsc.length === 0) return 0;
  const i = Math.floor(q * (sortedAsc.length - 1));
  return sortedAsc[i];
}

export function TraceChart({ times, durations, ages }: { times: number[]; durations: number[]; ages?: number[] }) {
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
  const ageVals = (ages ?? []).map((a) => a / 1000);
  // The age just before completion i: the previous completion's age grown by the interval between them. Ages and
  // completion times are aligned from the end (both keep the same recent history).
  const off = times.length - ageVals.length;
  const intervalBefore = (i: number) => (i + off >= 1 ? (times[i + off] - times[i + off - 1]) / 1000 : null);
  const peaks: (number | null)[] = ageVals.map((_, i) => {
    const gap = intervalBefore(i);
    return i > 0 && gap != null ? ageVals[i - 1] + gap : null;
  });

  const header = (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#a1a1aa', marginBottom: 4 }}>
      <span style={{ color: INTERVAL_COLOR }}>● interval</span>
      <span style={{ color: DURATION_COLOR }}>● run dur</span>
      {ages && <span style={{ color: AGE_COLOR }}>● data age</span>}
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
  const all = [...intervals, ...durs, ...ageVals, ...peaks.filter((v): v is number => v != null)];
  const sorted = [...all].sort((a, b) => a - b);
  const mean = all.reduce((a, b) => a + b, 0) / all.length;
  const yMax = Math.max(quantile(sorted, 0.9) + 0.4 * mean, 0.01);

  // Shared x-index by completion number (durations has one point per completion).
  const n = Math.max(durs.length, intervals.length + 1, ageVals.length);
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

  // Data age is a sawtooth: rise to each peak, drop at the completion. Dots mark the completions.
  const sawtooth = (vals: number[], color: string) => {
    if (vals.length === 0) return null;
    const pts: string[] = [];
    vals.forEach((v, i) => {
      const peak = peaks[i];
      if (peak != null) pts.push(`${x(i)},${y(peak)}`);
      pts.push(`${x(i)},${y(v)}`);
    });
    return (
      <>
        {pts.length > 1 && <polyline points={pts.join(' ')} fill="none" stroke={color} strokeWidth={1.5} />}
        {vals.map((v, i) => (
          <circle key={i} cx={x(i)} cy={y(v)} r={1.8} fill={color} />
        ))}
      </>
    );
  };

  // The time-averaged age over the last three intervals: across each one it grows linearly from the previous
  // completion's age, so it averages that age plus half the interval.
  const ageAverage = (() => {
    let area = 0;
    let span = 0;
    for (let i = Math.max(1, ageVals.length - 3); i < ageVals.length; i++) {
      const gap = intervalBefore(i);
      if (gap == null) continue;
      area += (ageVals[i - 1] + gap / 2) * gap;
      span += gap;
    }
    return span > 0 ? area / span : null;
  })();

  const meanLast3 = (vals: number[]) => {
    const last3 = vals.slice(-3);
    return last3.length ? last3.reduce((a, b) => a + b, 0) / last3.length : null;
  };
  const intMean = meanLast3(intervals);
  const durMean = meanLast3(durs);
  const ageMean = ageAverage;

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
        {meanLine(ageMean, AGE_COLOR)}
        {sawtooth(ageVals, AGE_COLOR)}
        {line(durs, DURATION_COLOR)}
        {line(intervals, INTERVAL_COLOR)}
      </svg>
    </div>
  );
}
