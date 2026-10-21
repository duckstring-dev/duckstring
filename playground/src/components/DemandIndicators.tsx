'use client';

import { formatAge, THEME_DEMAND } from '@/lib/store';

// The demand indicator shown to the left of a node's freshness readouts: a [≤ Ns] box with the requested
// freshness as an age, or "≤ ---" when the node holds no request.
export function DemandIndicators({ targetF, now }: { targetF: number | null; now: number }) {
  return (
    <span
      title={targetF !== null ? 'requested freshness' : 'no demand'}
      style={{
        fontSize: 9,
        lineHeight: 1,
        padding: '1px 3px',
        borderRadius: 3,
        border: `1px solid ${targetF !== null ? THEME_DEMAND : '#3f3f46'}`,
        color: targetF !== null ? THEME_DEMAND : '#52525b',
        whiteSpace: 'nowrap',
      }}
    >
      ≤{targetF !== null ? formatAge(targetF, now) : '---'}
    </span>
  );
}
