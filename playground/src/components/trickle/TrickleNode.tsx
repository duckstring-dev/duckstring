'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { THEME_BRAND } from '@/lib/store';
import { stageOf, stagedMarks, useTrickleStore, type PondKey } from '@/lib/trickle/store';
import { CellGrid } from './CellGrid';
import { FAINT, MONO, MUTED, TEXT } from './theme';

export const NODE_WIDTH = 260;
export const GRID_WIDTH = NODE_WIDTH - 24;
export const CELL = 4;
export const GAP = 1;

export const POND_INFO: Record<PondKey, { kind: string; what: string }> = {
  orders: { kind: 'append', what: 'order lines' },
  catalog: { kind: 'merge', what: 'one row per product' },
  priced: { kind: 'join', what: 'orders ⋈ catalog' },
  revenue: { kind: 'aggregate', what: 'totals per product' },
};

// The node's height for a table of `rows` rows, so the layout can make room for it.
export function nodeHeight(rows: number): number {
  const cols = Math.floor((GRID_WIDTH + GAP) / (CELL + GAP));
  return 46 + Math.max(1, Math.ceil(rows / cols)) * (CELL + GAP) + 44;
}

// Side handles sit level with the node's title, whatever its height, so edges between nodes with level tops
// run straight.
const handleAt = (vertical: boolean): React.CSSProperties => (vertical ? {} : { top: 20 });

// Which phase each node works in, for its highlight.
const WORKING: Record<PondKey, string[]> = {
  orders: ['sources'],
  catalog: ['sources'],
  priced: ['join'],
  revenue: ['aggregate'],
};

export const TrickleNode = memo(function TrickleNode({ data }: NodeProps) {
  const pond = data.pond as PondKey;
  const vertical = data.vertical as boolean;
  const result = useTrickleStore((s) => s.result);
  const phase = useTrickleStore((s) => s.phase);
  const ringed = useTrickleStore((s) => s.ring.includes(pond));
  const view = result?.[pond];
  const stage = stageOf(pond, phase);
  const working = WORKING[pond].includes(phase);
  const info = POND_INFO[pond];

  let status = 'empty';
  if (view && result) {
    if (stage === 'before' || stage === 'folded') status = `${view.ids.filter((id) => (stage === 'before' ? view.marks.get(id) !== 'added' : view.marks.get(id) !== 'removed')).length.toLocaleString()} rows`;
    else if (result.bootstrap) status = `${view.size.toLocaleString()} rows, all new`;
    else {
      const bits = [`${view.size.toLocaleString()} rows`];
      if (view.added) bits.push(`+${view.added}`);
      if (view.updated) bits.push(`${view.updated} changed`);
      if (view.removed) bits.push(`−${view.removed}`);
      if (stage === 'full' && view.read) bits.push(`${view.read} took part`);
      if (bits.length === 1) bits.push('no change');
      status = bits.join(' · ');
    }
  }

  return (
    <div
      data-testid={`node-${pond}`}
      className={ringed ? 'ds-tour-ring' : undefined}
      style={{
        width: NODE_WIDTH,
        boxSizing: 'border-box',
        border: `2px solid ${working ? THEME_BRAND : '#3f3f46'}`,
        boxShadow: working ? `0 0 0 4px ${THEME_BRAND}30` : undefined,
        borderRadius: 10,
        background: working ? '#0f2a30' : '#16161b',
        padding: 10,
        transition: 'border-color 200ms, background 200ms, box-shadow 200ms',
        userSelect: 'none',
      }}
    >
      {pond !== 'orders' && pond !== 'catalog' && (
        <Handle type="target" position={vertical ? Position.Top : Position.Left} style={{ background: '#52525b', ...handleAt(vertical) }} />
      )}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 }}>
        <span style={{ fontFamily: MONO, fontSize: 14, fontWeight: 700, color: TEXT }}>{pond}</span>
        <span style={{ fontFamily: MONO, fontSize: 10, color: working ? THEME_BRAND : FAINT, border: `1px solid ${working ? THEME_BRAND : '#3f3f46'}`, borderRadius: 4, padding: '0 5px' }}>
          {info.kind}
        </span>
        <span style={{ fontSize: 11, color: FAINT, marginLeft: 'auto' }}>{info.what}</span>
      </div>
      {view ? (
        <CellGrid marks={stagedMarks(view, stage)} label={`${pond}: ${status}`} cell={CELL} gap={GAP} />
      ) : (
        <div style={{ height: CELL, background: '#2c2b30' }} />
      )}
      <div data-testid={`node-${pond}-status`} style={{ fontFamily: MONO, fontSize: 11, color: MUTED, marginTop: 8, minHeight: 15 }}>
        {status}
      </div>
      {pond !== 'revenue' && (
        <Handle type="source" position={vertical ? Position.Bottom : Position.Right} style={{ background: '#52525b', ...handleAt(vertical) }} />
      )}
    </div>
  );
});

