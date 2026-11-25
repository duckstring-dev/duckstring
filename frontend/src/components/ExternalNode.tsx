'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore } from '@/lib/store';
import { externalApp, externalLabel } from '@/lib/layout';
import { TableIcon } from './PondNode';

// A declared external source ([pond] external; plans/records-and-media.md): where a Pond's data ultimately
// comes from, drawn like an App node but dotted and quiet, since Duckstring doesn't run it. An App line's data
// (`app:name@major`) carries a table icon opening the viewer on its Records and Media. Visual only: no state.
export const ExternalNode = memo(function ExternalNode({ data }: NodeProps) {
  const ext = data.external as string;
  const vertical = data.vertical as boolean | undefined;
  const openDataViewer = useLiveStore((s) => s.openDataViewer);
  const app = externalApp(ext);
  return (
    <div
      title={app ? `${app.name} v${app.major}'s Records and Media` : 'An external source'}
      style={{
        width: '100%', height: '100%', boxSizing: 'border-box', borderRadius: 16, position: 'relative',
        border: '2px dotted #52525b', background: '#0f0f1499', display: 'flex', flexDirection: 'column',
        justifyContent: 'center', padding: '0 14px', fontFamily: 'ui-monospace, SFMono-Regular, monospace',
        userSelect: 'none',
      }}
    >
      {app && (
        <span
          role="button"
          title="View its Records and Media"
          onClick={(e) => {
            e.stopPropagation();
            openDataViewer(`${app.name}@${app.major}`);
          }}
          style={{
            position: 'absolute', top: 7, right: 9, lineHeight: 0, padding: 3, borderRadius: 5,
            cursor: 'pointer', color: '#a1a1aa', background: '#0f0f14cc',
          }}
        >
          <TableIcon />
        </span>
      )}
      <span style={{ color: '#d4d4d8', fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden',
                     textOverflow: 'ellipsis', paddingRight: app ? 22 : 0 }}>
        {externalLabel(ext)}
      </span>
      <span style={{ color: '#71717a', fontSize: 11 }}>{app ? 'App data' : 'external'}</span>
      <Handle id="out" type="source" position={vertical ? Position.Bottom : Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
