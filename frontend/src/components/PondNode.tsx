'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore, formatAge, stateColor, nodeFill } from '@/lib/store';
import { DemandIndicators } from './DemandIndicators';

export const PondNode = memo(function PondNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  // TB (mobile) layout: flow enters at the top and leaves at the bottom; the trigger pill
  // hangs below the outlet, so its edge also lands on the bottom.
  const vertical = data.vertical as boolean | undefined;
  const pond = useLiveStore((s) => s.ponds[pondId]);
  const view = useLiveStore((s) => s.pondViews[pondId]);
  const info = useLiveStore((s) => s.pondInfo[pondId]);
  const selectedPondId = useLiveStore((s) => s.selectedPondId);
  const selectPond = useLiveStore((s) => s.selectPond);
  const now = useLiveStore((s) => s.now);

  if (!pond || !view) return null;

  const borderColor = stateColor(view);
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
        // A Pond Draw (fed by a duct) is drawn with a dashed border to set it apart from a local Pond.
        border: `2px ${pond.isDraw ? 'dashed' : 'solid'} ${borderColor}`,
        boxShadow: isSelected ? `0 0 0 3px ${borderColor}40` : undefined,
        borderRadius: 10,
        background: nodeFill(borderColor),
        cursor: 'pointer',
        position: 'relative',
        boxSizing: 'border-box',
        userSelect: 'none',
      }}
    >
      <Handle id="in" type="target" position={vertical ? Position.Top : Position.Left} style={{ background: '#52525b' }} />
      <Handle
        id="trigger-in"
        type="target"
        position={vertical ? Position.Bottom : Position.Right}
        style={{ background: '#52525b', opacity: 0 }}
      />

      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 64,
          padding: '6px 12px',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          gap: 5,
          // No divider on a Draw — it's header-only, with no ripple area beneath to separate.
          borderBottom: pond.isDraw ? 'none' : `1px solid ${borderColor}30`,
        }}
      >
        <span style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          {pond.isDraw && (
            <span style={{ fontSize: 10, fontWeight: 700, color: borderColor, letterSpacing: '0.06em', flexShrink: 0 }}>
              [DRAW]
            </span>
          )}
          <span style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7', letterSpacing: '0.04em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {pond.name}
          </span>
          {info && <span style={{ fontSize: 10, color: '#52525b', whiteSpace: 'nowrap' }}>v{info.version}</span>}
        </span>
        <span style={{ fontSize: 11, color: '#71717a', display: 'flex', alignItems: 'center', gap: 6 }}>
          <DemandIndicators hasPull={view.hasPull} targetF={view.targetF} now={now} />
          <span style={{ color: '#a1a1aa' }}>↑{formatAge(view.startF, now)} ({view.runsStarted})</span>
          <span>✓{formatAge(view.endF, now)} ({view.runsCompleted})</span>
        </span>
      </div>

      <Handle id="out" type="source" position={vertical ? Position.Bottom : Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
