'use client';

import { useState } from 'react';
import { THEME_DANGER, THEME_SUCCESS } from '@/lib/store';
import { BORDER, FAINT, MONO, MUTED, PANEL, TEXT } from './theme';

type Row = (string | number)[];

const EXAMPLES: { label: string; rows: Row[]; note: string }[] = [
  { label: 'Add a product', rows: [[57, '8.50', 1]], note: 'A new row arrives with weight +1.' },
  {
    label: 'Change a price',
    rows: [[42, '10.00', -1], [42, '12.00', 1]],
    note: 'An update removes the old row and adds the new one.',
  },
  { label: 'Remove a product', rows: [[42, '10.00', -1]], note: 'A removal carries the whole row, with weight -1.' },
];

function Weight({ w }: { w: number }) {
  return <span style={{ color: w > 0 ? THEME_SUCCESS : THEME_DANGER, fontWeight: 700 }}>{w > 0 ? `+${w}` : w}</span>;
}

function ZTable({ cols, rows, caption }: { cols: string[]; rows: Row[]; caption?: string }) {
  return (
    <table style={{ borderCollapse: 'collapse', fontFamily: MONO, fontSize: 12, width: '100%' }}>
      {caption && (
        <caption style={{ captionSide: 'top', textAlign: 'left', color: FAINT, fontSize: 11, paddingBottom: 4 }}>{caption}</caption>
      )}
      <thead>
        <tr>
          {cols.map((c) => (
            <th key={c} style={{ textAlign: 'left', color: MUTED, fontWeight: 500, padding: '3px 8px 3px 0', borderBottom: `1px solid ${BORDER}` }}>
              {c}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((v, j) => (
              <td key={j} style={{ padding: '3px 8px 3px 0', color: TEXT }}>
                {j === r.length - 1 && typeof v === 'number' ? <Weight w={v} /> : v}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const card: React.CSSProperties = { background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8, padding: 14 };

export function ZSetFigure() {
  const [i, setI] = useState(1);
  const ex = EXAMPLES[i];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={card}>
        <div role="tablist" style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
          {EXAMPLES.map((e, j) => (
            <button key={e.label} role="tab" aria-selected={j === i} onClick={() => setI(j)} style={pill(j === i)}>
              {e.label}
            </button>
          ))}
        </div>
        <ZTable cols={['product_id', 'unit_price', 'weight']} rows={ex.rows} caption="the change to catalog" />
        <p style={{ margin: '10px 0 0', color: MUTED, fontSize: 13 }}>{ex.note}</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2" style={{ gap: 14 }}>
        <div style={card}>
          <h4 style={h4}>Combining changes adds weights</h4>
          <ZTable cols={['product_id', 'unit_price', 'weight']} rows={[[42, '10.00', 1]]} caption="catalog before" />
          <div style={{ height: 8 }} />
          <ZTable cols={['product_id', 'unit_price', 'weight']} rows={[[42, '10.00', -1], [42, '12.00', 1]]} caption="+ the change" />
          <div style={{ height: 8 }} />
          <ZTable cols={['product_id', 'unit_price', 'weight']} rows={[[42, '12.00', 1]]} caption="= catalog after" />
          <p style={{ margin: '10px 0 0', color: MUTED, fontSize: 13 }}>The two 10.00 rows sum to zero, so that row no longer exists.</p>
        </div>
        <div style={card}>
          <h4 style={h4}>Joining multiplies weights</h4>
          <ZTable cols={['order_id', 'product_id', 'weight']} rows={[[7, 42, 1]]} caption="an order line" />
          <div style={{ height: 8 }} />
          <ZTable cols={['product_id', 'unit_price', 'weight']} rows={[[42, '10.00', -1], [42, '12.00', 1]]} caption="⋈ the change to catalog" />
          <div style={{ height: 8 }} />
          <ZTable cols={['order_id', 'unit_price', 'weight']} rows={[[7, '10.00', -1], [7, '12.00', 1]]} caption="= the change to priced" />
          <p style={{ margin: '10px 0 0', color: MUTED, fontSize: 13 }}>
            +1 × -1 gives -1: the old priced line is removed and the new one added, with no special handling.
          </p>
        </div>
      </div>
    </div>
  );
}

const h4: React.CSSProperties = { margin: '0 0 10px', fontSize: 13, fontWeight: 600, color: TEXT };

export function pill(active: boolean): React.CSSProperties {
  return {
    background: active ? '#06c4e622' : 'transparent',
    border: `1px solid ${active ? '#06c4e6' : BORDER}`,
    color: active ? '#06c4e6' : MUTED,
    borderRadius: 999,
    padding: '5px 11px',
    fontSize: 12,
    fontFamily: MONO,
    cursor: 'pointer',
    minHeight: 30,
  };
}
