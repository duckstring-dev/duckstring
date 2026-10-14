'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { usePlaygroundStore, refreshLabel, THEME_DEMAND, nodeFill } from '@/lib/store';

// A Pond's standing Refreshes, as one pill beside it: its floating Refresh (or first anchored one) and how many
// more. Clicking it selects the Pond, whose sidebar lists them.
export const TriggerNode = memo(function TriggerNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  // TB (mobile) layout: the pill hangs below its pond, so the edge leaves from the top.
  const vertical = data.vertical as boolean | undefined;
  const refreshes = usePlaygroundStore((s) => s.refreshes[pondId]);
  const selected = usePlaygroundStore((s) => s.selectedPondId === pondId && !s.selectedRippleId);
  const selectPond = usePlaygroundStore((s) => s.selectPond);

  if (!refreshes?.length) return null;
  const first = refreshes.find((r) => r.startAnchor == null) ?? refreshes[0];
  const more = refreshes.length - 1;
  const label = `${refreshLabel(first)}${more ? ` · +${more}` : ''}`;
  const color = THEME_DEMAND;

  return (
    <div
      data-testid="refresh-pill"
      onClick={(e) => {
        e.stopPropagation();
        selectPond(pondId);
      }}
      style={{
        // Fill the node wrapper so the handles (positioned relative to it) sit on the visible pill's edge.
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
        userSelect: 'none',
      }}
    >
      <Handle type="source" position={vertical ? Position.Top : Position.Left} style={{ background: color }} />
      <span style={{ fontSize: 12, fontWeight: 600, color }}>{label}</span>
    </div>
  );
});
