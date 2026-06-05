'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { usePlaygroundStore, getRippleVisualState, formatAge, pushTargetF, STATE_COLORS } from '@/lib/store';
import { DemandIndicators } from './DemandIndicators';

export const RippleNode = memo(function RippleNode({ data }: NodeProps) {
  const rippleId = data.rippleId as string;
  const ripple = usePlaygroundStore((s) => s.ripples[rippleId]);
  const rs = usePlaygroundStore((s) => s.rippleStates[rippleId]);
  const selectedRippleId = usePlaygroundStore((s) => s.selectedRippleId);
  const selectRipple = usePlaygroundStore((s) => s.selectRipple);
  const now = usePlaygroundStore((s) => s.now);

  if (!ripple || !rs) return null;

  const visualState = getRippleVisualState(rs);
  const borderColor = STATE_COLORS[visualState];
  const isSelected = selectedRippleId === rippleId;
  // Started-run freshness: in-flight start while running, else last completed freshness.
  const startedF = rs.isRunning ? rs.startF : rs.endF;

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
        background: '#1a1a1f',
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
        <DemandIndicators hasPull={rs.hasPull} targetF={pushTargetF(rs.targets)} now={now} />
        <span style={{ color: '#a1a1aa' }}>↑{formatAge(startedF, now)} ({rs.runsStarted})</span>
        <span>✓{formatAge(rs.endF, now)} ({rs.runsCompleted})</span>
      </span>
      <div style={{ fontSize: 11, color: borderColor, display: 'flex', gap: 6, whiteSpace: 'nowrap' }}>
        <span style={{ color: '#a1a1aa' }}>
          {rs.lastDurationMs != null ? `${(rs.lastDurationMs / 1000).toFixed(1)}s` : '—'}
        </span>
        <span style={{ color: '#52525b' }}>|</span>
        <span style={{ color: '#71717a' }}>~{(ripple.durationMs / 1000).toFixed(1)}s</span>
      </div>
      <Handle type="source" position={Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
