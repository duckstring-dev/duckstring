'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { usePlaygroundStore, getRippleVisualState, STATE_COLORS } from '@/lib/store';

export const RippleNode = memo(function RippleNode({ data }: NodeProps) {
  const rippleId = data.rippleId as string;
  const ripple = usePlaygroundStore((s) => s.ripples[rippleId]);
  const rs = usePlaygroundStore((s) => s.rippleStates[rippleId]);
  const ps = usePlaygroundStore((s) => s.pondStates[ripple?.pondId ?? '']);
  const selectedRippleId = usePlaygroundStore((s) => s.selectedRippleId);
  const selectRipple = usePlaygroundStore((s) => s.selectRipple);

  if (!ripple || !rs || !ps) return null;

  const visualState = getRippleVisualState(rs, ps);
  const borderColor = STATE_COLORS[visualState];
  const isSelected = selectedRippleId === rippleId;
  const displayGen = rs.isRunning ? rs.generationStarted : rs.generationCompleted;

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
        width: 140,
        height: 60,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        gap: 2,
        userSelect: 'none',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: '#52525b' }} />
      <div style={{ fontSize: 12, fontWeight: 600, color: '#e4e4e7', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {ripple.name}
      </div>
      <div style={{ fontSize: 11, color: borderColor, display: 'flex', gap: 6 }}>
        <span>gen {displayGen}</span>
        <span style={{ color: '#52525b' }}>·</span>
        <span style={{ color: '#71717a' }}>{(ripple.durationMs / 1000).toFixed(1)}s</span>
      </div>
      <Handle type="source" position={Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
