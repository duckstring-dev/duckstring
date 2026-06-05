'use client';

import { formatAge } from '@/lib/store';

// Demand indicators shown to the left of a node's freshness readouts:
//   • green dot  — holds pull demand
//   • [≤ Ns] box — push demand (target freshness as an age), or "≤ —" when no push target
export function DemandIndicators({
  hasPull,
  targetF,
  now,
}: {
  hasPull: boolean;
  targetF: number | null;
  now: number;
}) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <span
        title={hasPull ? 'pull demand' : 'no pull demand'}
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: hasPull ? '#22c55e' : 'transparent',
          border: `1px solid ${hasPull ? '#22c55e' : '#3f3f46'}`,
          flexShrink: 0,
        }}
      />
      <span
        title={targetF !== null ? 'push target (max staleness)' : 'no push demand'}
        style={{
          fontSize: 9,
          lineHeight: 1,
          padding: '1px 3px',
          borderRadius: 3,
          border: `1px solid ${targetF !== null ? '#3b82f6' : '#3f3f46'}`,
          color: targetF !== null ? '#3b82f6' : '#52525b',
          whiteSpace: 'nowrap',
        }}
      >
        ≤{targetF !== null ? formatAge(targetF, now) : '—'}
      </span>
    </span>
  );
}
