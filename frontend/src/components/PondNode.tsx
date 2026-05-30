'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { usePlaygroundStore, getPondVisualState, STATE_COLORS } from '@/lib/store';

export const PondNode = memo(function PondNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  const pond = usePlaygroundStore((s) => s.ponds[pondId]);
  const ps = usePlaygroundStore((s) => s.pondStates[pondId]);
  const selectedPondId = usePlaygroundStore((s) => s.selectedPondId);
  const selectPond = usePlaygroundStore((s) => s.selectPond);
  const pulseTagGen = usePlaygroundStore((s) => s.pulseTags[pondId]);

  if (!pond || !ps) return null;

  const visualState = getPondVisualState(ps);
  const borderColor = STATE_COLORS[visualState];
  const isSelected = selectedPondId === pondId;
  const showPulseTag = pulseTagGen !== undefined && ps.generationCompleted <= pulseTagGen;

  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        selectPond(pondId);
      }}
      style={{
        width: '100%',
        height: '100%',
        border: `2px solid ${borderColor}`,
        boxShadow: isSelected ? `0 0 0 3px ${borderColor}40` : undefined,
        borderRadius: 10,
        background: '#15151a',
        cursor: 'pointer',
        position: 'relative',
        boxSizing: 'border-box',
        userSelect: 'none',
      }}
    >
      <Handle id="in" type="target" position={Position.Left} style={{ background: '#52525b' }} />
      <Handle
        id="trigger-in"
        type="target"
        position={Position.Right}
        style={{ background: '#52525b', opacity: 0 }}
      />

      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 44,
          padding: '8px 12px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: `1px solid ${borderColor}30`,
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7', letterSpacing: '0.04em' }}>
          {pond.name}
        </span>
        <span style={{ fontSize: 11, color: '#71717a', display: 'flex', gap: 6 }}>
          <span style={{ color: '#a1a1aa' }}>↑{ps.generationStarted}</span>
          <span>✓{ps.generationCompleted}</span>
        </span>
      </div>

      {showPulseTag && (
        <div
          style={{
            position: 'absolute',
            top: -10,
            right: 8,
            fontSize: 10,
            fontWeight: 700,
            color: '#3b82f6',
            border: '1px solid #3b82f6',
            borderRadius: 10,
            padding: '1px 7px',
            background: '#0f0f14',
          }}
        >
          Pulse
        </div>
      )}

      <Handle id="out" type="source" position={Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
