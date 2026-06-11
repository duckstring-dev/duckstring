'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { usePlaygroundStore, formatDuration, THEME_PULL, THEME_PUSH, nodeFill } from '@/lib/store';

export const TriggerNode = memo(function TriggerNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  // TB (mobile) layout: the pill hangs below its pond, so the edge leaves from the top.
  const vertical = data.vertical as boolean | undefined;
  const trigger = usePlaygroundStore((s) => s.triggers[pondId]);
  const selectedTriggerId = usePlaygroundStore((s) => s.selectedTriggerId);
  const selectTrigger = usePlaygroundStore((s) => s.selectTrigger);

  if (!trigger) return null;

  const isWave = trigger.kind === 'wave';
  const color = isWave ? THEME_PULL : THEME_PUSH;
  const label = isWave ? 'Wave' : `Tide (≤${formatDuration(trigger.stalenessMs ?? 1000)})`;

  const isSelected = selectedTriggerId === pondId;

  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        selectTrigger(pondId);
      }}
      style={{
        // Fill the node wrapper (TRIGGER_W × TRIGGER_H) so the handles — positioned
        // relative to the wrapper — sit on the visible pill's edge.
        width: '100%',
        height: '100%',
        boxSizing: 'border-box',
        border: `2px solid ${color}`,
        boxShadow: isSelected ? `0 0 0 2px ${color}` : undefined,
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
      <Handle
        type="source"
        position={vertical ? Position.Top : Position.Left}
        style={{ background: color }}
      />
      <span style={{ fontSize: 12, fontWeight: 600, color }}>{label}</span>
    </div>
  );
});
