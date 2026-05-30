'use client';

// Run-cadence trace: time between consecutive run ends, full history compressed to fit.
// A dashed horizontal line marks the mean of the last 3 intervals, with a callout at right.
export function TraceChart({ times }: { times: number[] }) {
  const W = 256;
  const H = 96;
  const padL = 6;
  const padR = 44; // room for the mean callout
  const padT = 8;
  const padB = 14;

  // Intervals (seconds) between consecutive completions.
  const gaps: number[] = [];
  for (let i = 1; i < times.length; i++) gaps.push((times[i] - times[i - 1]) / 1000);

  const label = (
    <div style={{ fontSize: 11, color: '#a1a1aa', marginBottom: 4 }}>Interval between runs (s)</div>
  );

  if (gaps.length === 0) {
    return (
      <div>
        {label}
        <div style={{ fontSize: 11, color: '#52525b', padding: '24px 0', textAlign: 'center' }}>
          Needs 2+ completed runs.
        </div>
      </div>
    );
  }

  const last3 = gaps.slice(-3);
  const mean = last3.reduce((a, b) => a + b, 0) / last3.length;
  const maxGap = Math.max(...gaps, mean) * 1.1 || 1;

  const x = (i: number) =>
    gaps.length === 1 ? padL : padL + (i / (gaps.length - 1)) * (W - padL - padR);
  const y = (v: number) => padT + (1 - v / maxGap) * (H - padT - padB);

  const pts = gaps.map((g, i) => `${x(i)},${y(g)}`).join(' ');
  const meanY = y(mean);

  return (
    <div>
      {label}
      <svg width={W} height={H} style={{ display: 'block' }}>
        {/* baseline */}
        <line x1={padL} y1={H - padB} x2={W - padR} y2={H - padB} stroke="#27272a" strokeWidth={1} />
        {/* mean of last 3 */}
        <line
          x1={padL}
          y1={meanY}
          x2={W - padR}
          y2={meanY}
          stroke="#f59e0b"
          strokeWidth={1}
          strokeDasharray="4 3"
        />
        <text x={W - padR + 4} y={meanY + 3} fontSize={10} fill="#f59e0b" fontWeight={700}>
          {mean.toFixed(1)}s
        </text>
        {/* series */}
        {gaps.length > 1 && <polyline points={pts} fill="none" stroke="#22c55e" strokeWidth={1.5} />}
        {gaps.map((g, i) => (
          <circle key={i} cx={x(i)} cy={y(g)} r={1.8} fill="#22c55e" />
        ))}
      </svg>
    </div>
  );
}
