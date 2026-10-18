'use client';

import { useState } from 'react';
import { THEME_BRAND, THEME_DANGER, THEME_SUCCESS } from '@/lib/store';
import { BORDER, FAINT, MONO, MUTED, TEXT } from './theme';

// The small figures in the tour's first chapter. They follow product 42 through three runs: added at 09:00,
// repriced at 10:00, removed at 11:00. Rows already in a table are grey; a change is in colour.

type Cell = string | number;
type Row = Cell[] | { cells: Cell[]; existing?: boolean };

const cellsOf = (r: Row) => (Array.isArray(r) ? r : r.cells);
const existing = (r: Row) => !Array.isArray(r) && !!r.existing;

function Weight({ w, muted }: { w: number; muted?: boolean }) {
  const colour = muted ? FAINT : w > 0 ? THEME_SUCCESS : THEME_DANGER;
  return <span style={{ color: colour, fontWeight: muted ? 400 : 700 }}>{w > 0 ? `+${w}` : w}</span>;
}

// A Z-set as a table. The weight is the column named `weight`.
export function ZTable({ cols, rows, caption, empty = 'no rows' }: { cols: string[]; rows: Row[]; caption?: string; empty?: string }) {
  const wi = cols.indexOf('weight');
  return (
    <table style={{ borderCollapse: 'collapse', fontFamily: MONO, fontSize: 11.5, width: '100%', tableLayout: 'fixed' }}>
      {caption && (
        <caption style={{ captionSide: 'top', textAlign: 'left', color: FAINT, fontSize: 10.5, paddingBottom: 3 }}>{caption}</caption>
      )}
      <thead>
        <tr>
          {cols.map((c) => (
            <th key={c} style={{ textAlign: 'left', color: MUTED, fontWeight: 500, padding: '2px 6px 2px 0', borderBottom: `1px solid ${BORDER}` }}>
              {c}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={cols.length} style={{ padding: '2px 0', color: FAINT, fontStyle: 'italic' }}>{empty}</td>
          </tr>
        )}
        {rows.map((r, i) => {
          const muted = existing(r);
          return (
            <tr key={i}>
              {cellsOf(r).map((v, j) => (
                <td key={j} style={{ padding: '2px 6px 2px 0', color: muted ? FAINT : TEXT }}>
                  {j === wi && typeof v === 'number' ? <Weight w={v} muted={muted} /> : v}
                </td>
              ))}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const PRODUCT = ['product_id', 'unit_price', 'weight'];
const LOGGED = ['product_id', 'unit_price', 'weight', 'freshness'];

// What catalog already holds: grey.
const HELD: Row[] = [{ cells: [41, '7.00', 1], existing: true }, { cells: [42, '10.00', 1], existing: true }];

const EXAMPLES: { label: string; rows: Row[] }[] = [
  { label: 'Add', rows: [...HELD, [57, '8.50', 1]] },
  { label: 'Update', rows: [...HELD, [42, '10.00', -1], [42, '12.00', 1]] },
  { label: 'Remove', rows: [...HELD, [42, '10.00', -1]] },
];

export function WeightedRows() {
  const [i, setI] = useState(1);
  return (
    <div>
      <div role="tablist" style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        {EXAMPLES.map((e, j) => (
          <button key={e.label} role="tab" aria-selected={i === j} onClick={() => setI(j)} style={chip(i === j)}>
            {e.label}
          </button>
        ))}
      </div>
      <ZTable cols={PRODUCT} rows={EXAMPLES[i].rows} caption="catalog (grey), then the change" />
    </div>
  );
}

// Product 42's changes over three runs, unconsolidated.
export const LOG: Row[] = [
  [42, '10.00', 1, '09:00'],
  [42, '10.00', -1, '10:00'],
  [42, '12.00', 1, '10:00'],
  [42, '12.00', -1, '11:00'],
];

export function ChangeLog() {
  return <ZTable cols={LOGGED} rows={LOG} caption="catalog's changes for product 42" />;
}

export function Consolidation() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <ZTable cols={LOGGED} rows={LOG} caption="the changes" />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 6 }}>
        <ZTable cols={PRODUCT} rows={[[42, '10.00', 1]]} caption="consolidated up to 09:00" />
        <ZTable cols={PRODUCT} rows={[[42, '12.00', 1]]} caption="up to 10:00: the two 10.00 rows sum to 0" />
        <ZTable cols={PRODUCT} rows={[]} caption="up to 11:00: every row sums to 0" empty="no rows: product 42 is gone" />
      </div>
    </div>
  );
}

export function JoinLog() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <ZTable cols={['order_id', 'product_id', 'weight', 'freshness']} rows={[[7, 42, 1, '08:00']]} caption="an order line" />
      <ZTable cols={LOGGED} rows={LOG} caption="⋈ catalog's changes" />
      <ZTable
        cols={['order_id', 'unit_price', 'weight', 'freshness']}
        rows={[[7, '10.00', 1, '09:00'], [7, '10.00', -1, '10:00'], [7, '12.00', 1, '10:00'], [7, '12.00', -1, '11:00']]}
        caption="= priced's changes"
      />
    </div>
  );
}

export function Rules() {
  const box: React.CSSProperties = { border: `1px solid ${BORDER}`, borderRadius: 6, padding: '8px 10px', fontFamily: MONO, fontSize: 12, color: TEXT };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={box}>
        <div style={{ color: FAINT, fontSize: 10.5, marginBottom: 4 }}>combining Z-sets adds weights</div>
        <Weight w={1} /> + <Weight w={-1} /> = 0 <span style={{ color: MUTED }}>→ the row is gone</span>
      </div>
      <div style={box}>
        <div style={{ color: FAINT, fontSize: 10.5, marginBottom: 4 }}>joining Z-sets multiplies weights</div>
        <Weight w={1} /> × <Weight w={-1} /> = <Weight w={-1} /> <span style={{ color: MUTED }}>→ the joined row is removed</span>
      </div>
    </div>
  );
}

export function chip(active: boolean): React.CSSProperties {
  return {
    background: active ? `${THEME_BRAND}22` : 'transparent',
    border: `1px solid ${active ? THEME_BRAND : BORDER}`,
    color: active ? THEME_BRAND : MUTED,
    borderRadius: 999,
    padding: '3px 10px',
    fontSize: 11.5,
    fontFamily: MONO,
    cursor: 'pointer',
    minHeight: 28,
  };
}
