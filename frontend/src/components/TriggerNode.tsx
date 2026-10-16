'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore, THEME_DEMAND, nodeFill } from '@/lib/store';
import type { RefreshView } from '@/lib/types';

// A Pond's standing Refreshes, as one pill beside it: the most frequent one, and how many more. Clicking it
// selects the Pond, whose sidebar lists and edits them.

// The most frequent Refresh: continuous first, then the shortest period.
function mostFrequent(refreshes: RefreshView[]): RefreshView {
  return [...refreshes].sort((a, b) => (a.continuous ? 0 : a.everyMs || Infinity) - (b.continuous ? 0 : b.everyMs || Infinity))[0];
}

export const TriggerNode = memo(function TriggerNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  // TB (mobile) layout: the pill hangs below its pond, so the edge leaves from the top.
  const vertical = data.vertical as boolean | undefined;
  const refreshes = useLiveStore((s) => s.refreshes[pondId]);
  const selected = useLiveStore((s) => s.selectedPondId === pondId && !s.selectedRippleId);
  const selectPond = useLiveStore((s) => s.selectPond);

  if (!refreshes?.length) return null;

  const more = refreshes.length - 1;
  const label = `${mostFrequent(refreshes).name}${more ? ` · +${more}` : ''}`;
  const color = THEME_DEMAND;

  return (
    <div
      title={refreshes.map((r) => r.name).join('\n')}
      onClick={(e) => {
        e.stopPropagation();
        selectPond(pondId);
      }}
      style={{
        // Fill the node wrapper (TRIGGER_W × TRIGGER_H) so the handles — positioned
        // relative to the wrapper — sit on the visible pill's edge.
        width: '100%',
        height: '100%',
        boxSizing: 'border-box',
        border: `2px solid ${color}`,
        boxShadow: selected ? `0 0 0 2px ${color}` : undefined,
        borderRadius: 20,
        padding: '4px 12px',
        background: nodeFill(color),
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 4,
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        userSelect: 'none',
      }}
    >
      <Handle type="source" position={vertical ? Position.Top : Position.Left} style={{ background: color }} />
      <span style={{ fontSize: 12, fontWeight: 600, color, overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</span>
    </div>
  );
});
