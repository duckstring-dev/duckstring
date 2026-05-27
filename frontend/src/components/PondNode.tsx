'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useShallow } from 'zustand/shallow';
import { usePlaygroundStore, getRippleVisualState, STATE_COLORS } from '@/lib/store';

export const PondNode = memo(function PondNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  const pond = usePlaygroundStore((s) => s.ponds[pondId]);

  // useShallow prevents infinite-loop from new array on every call
  const rippleIds = usePlaygroundStore(
    useShallow((s) =>
      Object.values(s.ripples)
        .filter((r) => r.pondId === pondId)
        .map((r) => r.id)
    )
  );
  const rippleStates = usePlaygroundStore((s) => s.rippleStates);
  const selectedPondId = usePlaygroundStore((s) => s.selectedPondId);
  const selectPond = usePlaygroundStore((s) => s.selectPond);

  if (!pond) return null;

  const STATE_PRIORITY = { running: 3, stopped: 2, queued: 1, idle: 0 };
  let worstState: 'running' | 'stopped' | 'queued' | 'idle' = 'idle';
  let startedGen = 0;
  let completedGen = 0;

  for (const rid of rippleIds) {
    const rs = rippleStates[rid];
    if (!rs) continue;
    const vs = getRippleVisualState(rs);
    if (STATE_PRIORITY[vs] > STATE_PRIORITY[worstState]) worstState = vs;
    completedGen = Math.max(completedGen, rs.generation);
    if (rs.isRunning) startedGen = Math.max(startedGen, rs.generation + 1);
    else startedGen = Math.max(startedGen, rs.generation);
  }

  const borderColor = STATE_COLORS[worstState];
  const isSelected = selectedPondId === pondId;

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
          <span style={{ color: '#a1a1aa' }}>↑{startedGen}</span>
          <span>✓{completedGen}</span>
        </span>
      </div>

      <Handle id="out" type="source" position={Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
