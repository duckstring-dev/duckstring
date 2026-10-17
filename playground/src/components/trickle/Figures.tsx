'use client';

import { useState } from 'react';
import { THEME_BRAND, THEME_DANGER, THEME_SUCCESS } from '@/lib/store';
import { BORDER, FAINT, MONO, MUTED, TEXT } from './theme';

// The small figures the tour's first chapter shows: changes as weighted rows, and the two rules that make
// them work (combining adds weights, joining multiplies them).

type Row = (string | number)[];

function Weight({ w }: { w: number }) {
  return <span style={{ color: w > 0 ? THEME_SUCCESS : THEME_DANGER, fontWeight: 700 }}>{w > 0 ? `+${w}` : w}</span>;
}

export function ZTable({ cols, rows, caption }: { cols: string[]; rows: Row[]; caption?: string }) {
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
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((v, j) => (
              <td key={j} style={{ padding: '2px 6px 2px 0', color: TEXT }}>
                {j === r.length - 1 && typeof v === 'number' ? <Weight w={v} /> : v}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const PRODUCT = ['product_id', 'unit_price', 'weight'];

const EXAMPLES: { label: string; rows: Row[] }[] = [
  { label: 'Add', rows: [[57, '8.50', 1]] },
  { label: 'Update', rows: [[42, '10.00', -1], [42, '12.00', 1]] },
  { label: 'Remove', rows: [[42, '10.00', -1]] },
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
      <ZTable cols={PRODUCT} rows={EXAMPLES[i].rows} caption="a change to catalog" />
    </div>
  );
}

export function CombineRule() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <ZTable cols={PRODUCT} rows={[[42, '10.00', 1]]} caption="catalog" />
      <ZTable cols={PRODUCT} rows={[[42, '10.00', -1], [42, '12.00', 1]]} caption="+ the change" />
      <ZTable cols={PRODUCT} rows={[[42, '12.00', 1]]} caption="= catalog after the run" />
    </div>
  );
}

export function JoinRule() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <ZTable cols={['order_id', 'product_id', 'weight']} rows={[[7, 42, 1]]} caption="an order line" />
      <ZTable cols={PRODUCT} rows={[[42, '10.00', -1], [42, '12.00', 1]]} caption="⋈ the change to catalog" />
      <ZTable cols={['order_id', 'unit_price', 'weight']} rows={[[7, '10.00', -1], [7, '12.00', 1]]} caption="= the change to priced" />
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
