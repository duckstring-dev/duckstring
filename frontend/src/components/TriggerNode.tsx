'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore, formatDuration } from '@/lib/store';

export const TriggerNode = memo(function TriggerNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  const trigger = useLiveStore((s) => s.triggers[pondId]);
  const selectedTriggerId = useLiveStore((s) => s.selectedTriggerId);
  const selectTrigger = useLiveStore((s) => s.selectTrigger);

  if (!trigger) return null;

  const isWave = trigger.kind === 'wave';
  const color = isWave ? '#22c55e' : '#3b82f6';
  const label = isWave ? 'Wave' : `Tide (≤${formatDuration(trigger.boundMs ?? 1000)})`;

  const isSelected = selectedTriggerId === pondId;

  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        selectTrigger(pondId);
      }}
      style={{
        border: `2px solid ${color}`,
        boxShadow: isSelected ? `0 0 0 2px ${color}` : undefined,
        borderRadius: 20,
        padding: '4px 12px',
        background: `${color}18`,
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        whiteSpace: 'nowrap',
        userSelect: 'none',
      }}
    >
      <Handle type="source" position={Position.Left} style={{ background: color }} />
      <span style={{ fontSize: 12, fontWeight: 600, color }}>{label}</span>
    </div>
  );
});
