'use client';

import { memo } from 'react';
import { type NodeProps } from '@xyflow/react';

// A labelled container framing one upstream Catchment's Ponds in the lineage overlay. Read-only:
// it's another Catchment's territory, shown for provenance.
export const CatchmentGroupNode = memo(function CatchmentGroupNode({ data }: NodeProps) {
  const name = (data.name as string | null) ?? null;
  const reachable = data.reachable as boolean;

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        border: `1px dashed ${reachable ? '#3f3f46' : '#7f1d1d'}`,
        borderRadius: 12,
        background: reachable ? 'rgba(255,255,255,0.015)' : 'rgba(127,29,29,0.06)',
        boxSizing: 'border-box',
      }}
    >
      <div
        style={{
          position: 'absolute',
          top: 8,
          left: 12,
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: reachable ? '#71717a' : '#b91c1c',
        }}
      >
        {name || 'Catchment'}
        {!reachable && <span style={{ marginLeft: 6, fontWeight: 400 }}>· unreachable</span>}
      </div>
    </div>
  );
});
