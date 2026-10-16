'use client';

import { formatAge, THEME_DEMAND } from '@/lib/store';

// The demand box shown to the left of a node's freshness readouts: the oldest freshness the node has been
// asked for and not yet met, as an age ("≤ Ns"), or "≤ ---" when it holds no demand.
export function DemandIndicators({ demandF, now }: { demandF: number | null; now: number }) {
  return (
    <span
      title={demandF !== null ? 'asked for data at least this fresh' : 'no demand'}
      style={{
        fontSize: 9,
        lineHeight: 1,
        padding: '1px 3px',
        borderRadius: 3,
        border: `1px solid ${demandF !== null ? THEME_DEMAND : '#3f3f46'}`,
        color: demandF !== null ? THEME_DEMAND : '#52525b',
        whiteSpace: 'nowrap',
      }}
    >
      ≤{demandF !== null ? formatAge(demandF, now) : '---'}
    </span>
  );
}
