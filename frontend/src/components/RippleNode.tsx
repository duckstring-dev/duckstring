'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore, formatAge, stateColor, nodeFill } from '@/lib/store';
import { DemandIndicators } from './DemandIndicators';

export const RippleNode = memo(function RippleNode({ data }: NodeProps) {
  const rippleId = data.rippleId as string;
  const ripple = useLiveStore((s) => s.ripples[rippleId]);
  const view = useLiveStore((s) => s.rippleViews[rippleId]);
  const selectedRippleId = useLiveStore((s) => s.selectedRippleId);
  const selectRipple = useLiveStore((s) => s.selectRipple);
  const now = useLiveStore((s) => s.now);

  if (!ripple || !view) return null;

  const borderColor = stateColor(view);
  const isSelected = selectedRippleId === rippleId;
  // Started-run freshness: in-flight start while running, else last completed freshness.
  const startedF = view.status === 'running' ? view.startF : view.endF;

  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        selectRipple(rippleId);
      }}
      style={{
        border: `2px solid ${borderColor}`,
        boxShadow: isSelected ? `0 0 0 2px ${borderColor}` : undefined,
        borderRadius: 6,
        padding: '6px 10px',
        background: nodeFill(borderColor),
        cursor: 'pointer',
        width: '100%',
        height: 80,
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        gap: 4,
        userSelect: 'none',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: '#52525b' }} />
      <span style={{ fontSize: 12, fontWeight: 600, color: '#e4e4e7', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {ripple.name}
      </span>
      <span style={{ fontSize: 11, color: '#71717a', display: 'flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
        <DemandIndicators hasPull={view.hasPull} targetF={view.targetF} now={now} />
        <span style={{ color: '#a1a1aa' }}>↑{formatAge(startedF, now)} ({view.runsStarted})</span>
        <span>✓{formatAge(view.endF, now)} ({view.runsCompleted})</span>
      </span>
      <Handle type="source" position={Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
