'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore, formatAge, parseTs, STATE_COLORS, nodeFill } from '@/lib/store';

// A read-only Pond in an upstream Catchment's container (lineage overlay). No internal Ripples — it's
// another Catchment's detail; we show identity + freshness + state colour. A Draw there is dashed.
export const RemotePondNode = memo(function RemotePondNode({ data }: NodeProps) {
  const now = useLiveStore((s) => s.now);
  const name = data.name as string;
  const isDraw = data.isDraw as boolean;
  const vertical = data.vertical as boolean | undefined;
  const catchmentId = data.catchmentId as string;
  const pondId = data.pondId as string;

  // Live state (status/freshness) from the polled lineage, so it updates per tick without a relayout.
  const live = useLiveStore((s) =>
    s.lineage?.catchments.find((c) => c.id === catchmentId)?.ponds.find((p) => p.id === pondId)
  );
  const status = live?.status ?? 'idle';
  const endF = live?.end_f ?? null;
  const color = STATE_COLORS[status] ?? STATE_COLORS.idle;

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        border: `2px ${isDraw ? 'dashed' : 'solid'} ${color}`,
        borderRadius: 9,
        background: nodeFill(color),
        boxSizing: 'border-box',
        padding: '6px 10px',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        gap: 3,
        opacity: 0.92,
      }}
    >
      <Handle type="target" position={vertical ? Position.Top : Position.Left} style={{ background: '#52525b' }} />
      <span style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        {isDraw && (
          <span style={{ fontSize: 9, fontWeight: 700, color, letterSpacing: '0.06em', flexShrink: 0 }}>[DRAW]</span>
        )}
        <span style={{ fontSize: 12, fontWeight: 700, color: '#e4e4e7', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {name}
        </span>
      </span>
      <span style={{ fontSize: 10, color: '#71717a' }}>✓{formatAge(parseTs(endF), now)}</span>
      <Handle type="source" position={vertical ? Position.Bottom : Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
