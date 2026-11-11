'use client';

import { Fragment, useState } from 'react';
import { THEME_AMBER, THEME_DEMAND, THEME_RUNNING } from '@/lib/store';

// Run cadence, run duration and data age on one log y-axis. The run duration reads against the cadence
// (≈ the bottleneck): well below it, this node isn't the bottleneck; close to it, it is.
//
// Every series' markers are interactive. Hovering any marker of a run picks that run: a dashed vertical
// line joins its points and a static readout beneath the plot shows the previous → this completion times,
// the interval, the duration and the age (the same readout whichever marker is hovered, so no edge-clipped
// floating tooltip). Clicking one selects the run (opening it in Run Detail). Symmetrically, `selectedKey`
// enlarges the markers of the selected run; a run with no marker here (a different Pond) doesn't
// highlight. Intervals are indexed to the *later* run
// (the completion that ends the gap), so a run's markers share its x-position and any of them selects it.
//
// Data age is a sawtooth: at each completion it drops to the age of the data just published (completion time
// minus the run's freshness), then grows until the next one, so its peak is that age plus the interval. Its
// dashed average is weighted by time. An App has no run duration (its Runs are instant passes), so it plots
// interval and age only (`durations={false}`).
const INTERVAL_COLOR = THEME_AMBER; // run cadence (≈ the bottleneck under a continuous Refresh)
const DURATION_COLOR = THEME_RUNNING; // run duration, tied to the running state
const AGE_COLOR = THEME_DEMAND; // data age, tied to demand and freshness

export type TracePointView = { key: string; time: number; duration: number; age: number };

const clock = (msVal: number): string => {
  const d = new Date(msVal);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

export function TraceChart({
  points, selectedKey = null, onSelect, durations = true, empty = 'No completed runs yet.',
}: {
  points: TracePointView[];
  selectedKey?: string | null;
  onSelect?: (key: string) => void;
  durations?: boolean;
  empty?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 256;
  const H = 100;
  const padL = 6;
  const padR = 46;
  const padT = 10;
  const padB = 14;

  const times = points.map((p) => p.time);
  const durs = points.map((p) => p.duration / 1000); // one per run (s)
  // Interval (s) into run i from the previous completion — indexed to the later run (i ≥ 1).
  const interval = (i: number) => (times[i] - times[i - 1]) / 1000;
  const intervalVals: number[] = [];
  for (let i = 1; i < points.length; i++) intervalVals.push(interval(i));
  const ages = points.map((p) => Math.max(p.age, 0) / 1000); // a windowed Inlet's freshness can be ahead of it
  const peak = (i: number) => ages[i - 1] + interval(i); // the age just before completion i (i ≥ 1)

  const header = (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#a1a1aa', marginBottom: 4 }}>
      <span style={{ color: INTERVAL_COLOR }}>● interval</span>
      {durations && <span style={{ color: DURATION_COLOR }}>● run dur</span>}
      <span style={{ color: AGE_COLOR }}>● data age</span>
      <span style={{ color: '#71717a' }}>(s)</span>
    </div>
  );

  if (points.length === 0) {
    return (
      <div>
        {header}
        <div style={{ fontSize: 11, color: '#52525b', padding: '28px 0', textAlign: 'center' }}>{empty}</div>
      </div>
    );
  }

  // Log y-axis: durations/intervals routinely span orders of magnitude, so plot log10(value) over the
  // full [min, max] range (with a small margin). Log compresses the outliers itself, so there's no clip.
  const FLOOR = 1e-3; // 1 ms — below measurement resolution; guards log(0)
  const peaks: number[] = [];
  for (let i = 1; i < points.length; i++) peaks.push(peak(i));
  const all = [...intervalVals, ...(durations ? durs : []), ...ages, ...peaks];
  const positives = all.filter((v) => v > 0);
  const vMin = positives.length ? Math.max(Math.min(...positives), FLOOR) : FLOOR;
  const vMax = positives.length ? Math.max(...positives, vMin) : FLOOR;
  const logLo = Math.log10(vMin);
  const logHi = Math.log10(vMax);
  const margin = Math.max((logHi - logLo) * 0.08, 0.05);
  const lo = logLo - margin;
  const span = logHi + margin - lo;

  // Shared x-index by completion number (one point per run).
  const n = points.length;
  const x = (i: number) => (n <= 1 ? padL : padL + (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => {
    const t = span > 1e-9 ? (Math.log10(Math.max(v, FLOOR)) - lo) / span : 0.5;
    return padT + (1 - Math.min(Math.max(t, 0), 1)) * (H - padT - padB);
  };

  // Faint decade gridlines (powers of 10 in range) — the visual cue that the axis is logarithmic.
  const decades: number[] = [];
  for (let e = Math.ceil(logLo); e <= Math.floor(logHi); e++) decades.push(10 ** e);

  const meanLast3 = (vals: number[]) => {
    const last3 = vals.slice(-3);
    return last3.length ? last3.reduce((a, b) => a + b, 0) / last3.length : null;
  };
  // The time-averaged age over the last 3 intervals: across each one it grows linearly from the previous
  // completion's age, so it averages that age plus half the interval.
  const ageMean = (() => {
    let area = 0;
    let total = 0;
    for (let i = Math.max(1, n - 3); i < n; i++) {
      const gap = interval(i);
      area += (ages[i - 1] + gap / 2) * gap;
      total += gap;
    }
    return total > 0 ? area / total : null;
  })();
  // The dashed mean lines, with their labels in the right margin pushed apart so close means stay readable.
  const means = ([
    [meanLast3(intervalVals), INTERVAL_COLOR],
    [durations ? meanLast3(durs) : null, DURATION_COLOR],
    [ageMean, AGE_COLOR],
  ] as [number | null, string][]).filter((m): m is [number, string] => m[0] != null)
    .map(([m, color]) => ({ m, color, ly: y(m) + 3 }))
    .sort((a, b) => a.ly - b.ly);
  for (let k = 1; k < means.length; k++) means[k].ly = Math.max(means[k].ly, means[k - 1].ly + 10);
  const over = means.length ? means[means.length - 1].ly - (H - 1) : 0;
  if (over > 0) means.forEach((l) => { l.ly -= over; });

  const polyline = (coords: string[], color: string) =>
    coords.length > 1 ? <polyline points={coords.join(' ')} fill="none" stroke={color} strokeWidth={1.5} /> : null;
  const durLine = polyline(durs.map((v, i) => `${x(i)},${y(v)}`), DURATION_COLOR);
  const intLine = polyline(points.slice(1).map((_, k) => `${x(k + 1)},${y(interval(k + 1))}`), INTERVAL_COLOR);
  // Rise to each peak, drop at the completion.
  const ageLine = polyline(ages.flatMap((v, i) => [...(i >= 1 ? [`${x(i)},${y(peak(i))}`] : []), `${x(i)},${y(v)}`]),
                           AGE_COLOR);

  // A marker for one series of one run: a visible dot (enlarged when hovered/selected) + a wide hit area.
  const marker = (i: number, kind: 'dur' | 'int' | 'age', color: string) => {
    const cy = y(kind === 'dur' ? durs[i] : kind === 'int' ? interval(i) : ages[i]);
    const active = selectedKey === points[i].key || hover === i;
    return (
      <g key={kind}>
        <circle cx={x(i)} cy={cy} r={active ? 4 : 2} fill={color} stroke={active ? '#fff' : 'none'} strokeWidth={active ? 1 : 0} />
        <circle cx={x(i)} cy={cy} r={7} fill="transparent" style={{ cursor: onSelect ? 'pointer' : 'default' }}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover((h) => (h === i ? null : h))}
                onClick={onSelect ? () => onSelect(points[i].key) : undefined} />
      </g>
    );
  };

  return (
    <div>
      {header}
      <svg width={W} height={H} style={{ display: 'block' }}>
        {decades.map((val) => (
          <line key={val} x1={padL} y1={y(val)} x2={W - padR} y2={y(val)} stroke="#27272a" strokeWidth={0.5} strokeDasharray="1 3" />
        ))}
        <line x1={padL} y1={H - padB} x2={W - padR} y2={H - padB} stroke="#27272a" strokeWidth={1} />
        {means.map(({ m, color, ly }) => (
          <Fragment key={color}>
            <line x1={padL} y1={y(m)} x2={W - padR} y2={y(m)} stroke={color} strokeWidth={1} strokeDasharray="4 3" />
            <text x={W - padR + 4} y={ly} fontSize={10} fill={color} fontWeight={700}>{m.toFixed(1)}s</text>
          </Fragment>
        ))}
        {ageLine}
        {durations && durLine}
        {intLine}
        {/* The dashed connector joins the hovered run's points. */}
        {hover != null && (() => {
          const ys = [y(ages[hover]), ...(durations ? [y(durs[hover])] : []), ...(hover >= 1 ? [y(interval(hover))] : [])];
          return ys.length > 1 && (
            <line x1={x(hover)} y1={Math.min(...ys)} x2={x(hover)} y2={Math.max(...ys)}
                  stroke="#71717a" strokeWidth={1} strokeDasharray="3 2" />
          );
        })()}
        {points.map((p, i) => (
          <Fragment key={p.key}>
            {marker(i, 'age', AGE_COLOR)}
            {durations && marker(i, 'dur', DURATION_COLOR)}
            {i >= 1 && marker(i, 'int', INTERVAL_COLOR)}
          </Fragment>
        ))}
      </svg>
      {/* Static readout beneath the plot (never clipped) — same info whichever marker is hovered. */}
      <div style={{ marginTop: 6, minHeight: 47, fontSize: 10.5, lineHeight: 1.5, fontFamily: 'inherit' }}>
        {hover != null && points[hover] ? (
          <>
            <div style={{ color: '#a1a1aa' }}>
              {hover >= 1 ? `${clock(times[hover - 1])} → ${clock(times[hover])}` : clock(times[hover])}
            </div>
            {(hover >= 1 || durations) && (
              <div>
                {hover >= 1 && <span style={{ color: INTERVAL_COLOR }}>interval {interval(hover).toFixed(2)}s</span>}
                {hover >= 1 && durations && <span style={{ color: '#52525b' }}> · </span>}
                {durations && <span style={{ color: DURATION_COLOR }}>run {durs[hover].toFixed(2)}s</span>}
              </div>
            )}
            <div style={{ color: AGE_COLOR }}>
              age {ages[hover].toFixed(2)}s{hover >= 1 ? `, peak ${peak(hover).toFixed(2)}s` : ''}
            </div>
          </>
        ) : (
          <div style={{ color: '#3f3f46' }}>Hover a point for its timing.</div>
        )}
      </div>
    </div>
  );
}
