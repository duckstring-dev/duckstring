'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { usePlaygroundStore, getPondVisualState, formatAge, pushTargetF, stateColor, nodeFill, THEME_PUSH } from '@/lib/store';
import { DemandIndicators } from './DemandIndicators';

export const PondNode = memo(function PondNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  // TB (mobile) layout: flow enters at the top and leaves at the bottom; the trigger pill
  // hangs below the outlet, so its edge also lands on the bottom.
  const vertical = data.vertical as boolean | undefined;
  const pond = usePlaygroundStore((s) => s.ponds[pondId]);
  const ps = usePlaygroundStore((s) => s.pondStates[pondId]);
  // A Pond is running while any of its Ripples is (the Catchment's notion of a running Pond).
  const busy = usePlaygroundStore((s) =>
    Object.values(s.ripples).some((r) => r.pondId === pondId && s.rippleStates[r.id]?.isRunning)
  );
  const selectedPondId = usePlaygroundStore((s) => s.selectedPondId);
  const selectPond = usePlaygroundStore((s) => s.selectPond);
  const now = usePlaygroundStore((s) => s.now);
  const pulseTagGen = usePlaygroundStore((s) => s.pulseTags[pondId]);

  if (!pond || !ps) return null;

  const visualState = getPondVisualState(ps, busy);
  const borderColor = stateColor(visualState, ps.hasPull || ps.hasReceivedPull, pushTargetF(ps.targets));
  const isSelected = selectedPondId === pondId;
  const showPulseTag = pulseTagGen !== undefined && ps.runsCompleted <= pulseTagGen;

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
          borderBottom: `1px solid ${borderColor}30`,
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7', letterSpacing: '0.04em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {pond.name}
        </span>
        <span style={{ fontSize: 11, color: '#71717a', display: 'flex', alignItems: 'center', gap: 6 }}>
          <DemandIndicators hasPull={ps.hasPull || ps.hasReceivedPull} targetF={pushTargetF(ps.targets)} now={now} />
          <span style={{ color: '#a1a1aa' }}>↑{formatAge(ps.startF, now)} ({ps.runsStarted})</span>
          <span>✓{formatAge(ps.endF, now)} ({ps.runsCompleted})</span>
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
            color: THEME_PUSH,
            border: `1px solid ${THEME_PUSH}`,
            borderRadius: 10,
            padding: '1px 7px',
            background: '#0f0f14',
          }}
        >
          Pulse
        </div>
      )}

      <Handle id="out" type="source" position={vertical ? Position.Bottom : Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
