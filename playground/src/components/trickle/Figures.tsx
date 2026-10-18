'use client';

import { useState } from 'react';
import { THEME_BRAND, THEME_DANGER, THEME_SUCCESS } from '@/lib/store';
import { BORDER, FAINT, MONO, MUTED, TEXT } from './theme';

// The small figures in the tour's first chapter. They follow product 42 through three runs: added at 09:00,
// repriced at 10:00, removed at 11:00. Rows already in a table are grey; a change is in colour.

type Cell = string | number | React.ReactNode;
type Row = Cell[] | { cells: Cell[]; existing?: boolean };

const cellsOf = (r: Row) => (Array.isArray(r) ? r : r.cells);
const existing = (r: Row) => !Array.isArray(r) && !!r.existing;

function Weight({ w, muted }: { w: number; muted?: boolean }) {
  const colour = muted ? FAINT : w > 0 ? THEME_SUCCESS : w < 0 ? THEME_DANGER : TEXT;
  return <span style={{ color: colour, fontWeight: muted ? 400 : 700 }}>{w > 0 ? `+${w}` : w}</span>;
}

// A Z-set as a table. The weight is the column named `weight`. `selected` rows are highlighted, and `onSelect`
// makes rows tappable.
export function ZTable({ cols, rows, caption, empty = 'no rows', selected = [], onSelect }: {
  cols: string[]; rows: Row[]; caption?: string; empty?: string; selected?: number[]; onSelect?: (i: number) => void;
}) {
  const wi = cols.indexOf('weight');
  return (
    <table style={{ borderCollapse: 'collapse', fontFamily: MONO, fontSize: 11.5, width: '100%', tableLayout: 'fixed' }}>
      {caption && (
        <caption style={{ captionSide: 'top', textAlign: 'left', color: FAINT, fontSize: 10.5, paddingBottom: 3 }}>{caption}</caption>
      )}
      <thead>
        <tr>
          {cols.map((c) => (
            <th key={c} style={{ textAlign: 'left', color: MUTED, fontWeight: 500, padding: '1px 6px 1px 4px', borderBottom: `1px solid ${BORDER}` }}>
              {c}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={cols.length} style={{ padding: '2px 4px', color: FAINT, fontStyle: 'italic' }}>{empty}</td>
          </tr>
        )}
        {rows.map((r, i) => {
          const muted = existing(r);
          const on = selected.includes(i);
          return (
            <tr
              key={i}
              onClick={onSelect ? () => onSelect(i) : undefined}
              style={{ background: on ? `${THEME_BRAND}2a` : undefined, outline: on ? `1px solid ${THEME_BRAND}` : undefined, cursor: onSelect ? 'pointer' : undefined }}
            >
              {cellsOf(r).map((v, j) => (
                <td key={j} style={{ padding: '1px 6px 1px 4px', color: muted ? FAINT : TEXT }}>
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

const EXAMPLES: { label: string; rows: Row[] }[] = [
  { label: 'Add', rows: [{ cells: [41, '7.00', 1], existing: true }, [42, '10.00', 1]] },
  { label: 'Update', rows: [{ cells: [41, '7.00', 1], existing: true }, { cells: [42, '10.00', 1], existing: true }, [42, '10.00', -1], [42, '12.00', 1]] },
  { label: 'Remove', rows: [{ cells: [41, '7.00', 1], existing: true }, { cells: [42, '12.00', 1], existing: true }, [42, '12.00', -1]] },
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

// The unconsolidated catalog every later card uses: two rows already there (grey), then product 42 added at
// 09:00, repriced at 10:00 and removed at 11:00.
const UNCONSOLIDATED: { product: number; price: string; w: number; f: string; held?: boolean }[] = [
  { product: 41, price: '7.00', w: 1, f: '08:00', held: true },
  { product: 43, price: '9.50', w: 1, f: '08:00', held: true },
  { product: 42, price: '10.00', w: 1, f: '09:00' },
  { product: 42, price: '10.00', w: -1, f: '10:00' },
  { product: 42, price: '12.00', w: 1, f: '10:00' },
  { product: 42, price: '12.00', w: -1, f: '11:00' },
];

const unconsolidated = (withF: boolean): Row[] =>
  UNCONSOLIDATED.map((r) => ({ cells: withF ? [r.product, r.price, r.w, r.f] : [r.product, r.price, r.w], existing: r.held }));

export function ChangeLog() {
  return <ZTable cols={['product_id', 'unit_price', 'weight', 'freshness']} rows={unconsolidated(true)} caption="catalog, unconsolidated" />;
}

// The weights of identical rows and their total, e.g. `+1 -1 = 0`.
function Sum({ terms }: { terms: number[] }) {
  const total = terms.reduce((x, y) => x + y, 0);
  return (
    <span>
      {terms.map((t, i) => (
        <span key={i}>{i > 0 ? ' ' : ''}<Weight w={t} /></span>
      ))}
      <span style={{ color: MUTED }}> = </span>
      <Weight w={total} />
    </span>
  );
}

export function Consolidation() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <ZTable cols={PRODUCT} rows={unconsolidated(false)} caption="1. catalog, unconsolidated" />
      <ZTable
        cols={PRODUCT}
        rows={[
          { cells: [41, '7.00', 1], existing: true },
          { cells: [43, '9.50', 1], existing: true },
          [42, '10.00', <Sum key="a" terms={[1, -1]} />],
          [42, '12.00', <Sum key="b" terms={[1, -1]} />],
        ]}
        caption="2. sum the weights of identical rows"
      />
      <ZTable cols={PRODUCT} rows={[{ cells: [41, '7.00', 1], existing: true }, { cells: [43, '9.50', 1], existing: true }]} caption="3. drop rows that sum to 0" />
    </div>
  );
}

// The join, pair by pair: each result row comes from one order line and one catalog row with the same
// product_id, and weighs the product of their weights.
const ORDERS = [
  { order: 7, product: 42 },
  { order: 8, product: 41 },
];
const PAIRS = ORDERS.flatMap((o, oi) =>
  UNCONSOLIDATED.map((c, ci) => ({ oi, ci, o, c })).filter((p) => p.c.product === o.product),
).sort((a, b) => a.ci - b.ci);

export function JoinPairs() {
  const [sel, setSel] = useState(PAIRS.findIndex((p) => p.c.w < 0));
  const p = PAIRS[sel];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <ZTable cols={['order_id', 'product_id', 'weight']} rows={ORDERS.map((o) => [o.order, o.product, 1])} caption="orders" selected={[p.oi]} />
      <ZTable cols={PRODUCT} rows={unconsolidated(false)} caption="⋈ catalog (unconsolidated) on product_id" selected={[p.ci]} />
      <ZTable
        cols={['order_id', 'unit_price', 'pair', 'weight']}
        rows={PAIRS.map((q) => ({
          cells: [q.o.order, q.c.price, <span key="x" style={{ color: MUTED }}>+1 × {q.c.w > 0 ? '+1' : '-1'}</span>, q.c.w],
          existing: !!q.c.held,
        }))}
        caption="= priced (tap a row to see its pair)"
        selected={[sel]}
        onSelect={setSel}
      />
      <div style={{ fontSize: 11, color: MUTED, lineHeight: 1.5 }}>
        Product 43 has no order line, so it pairs with nothing. Consolidated, order 7 cancels out: the same as joining
        the consolidated tables.
      </div>
    </div>
  );
}

export function Rules() {
  const box: React.CSSProperties = { border: `1px solid ${BORDER}`, borderRadius: 6, padding: '8px 10px', fontFamily: MONO, fontSize: 12, color: TEXT, lineHeight: 1.6 };
  const label: React.CSSProperties = { color: FAINT, fontSize: 10.5, marginBottom: 2 };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={box}>
        <div style={label}>a join distributes over a sum</div>
        (A + ΔA) ⋈ B = A ⋈ B + ΔA ⋈ B
      </div>
      <div style={box}>
        <div style={label}>so, like numbers</div>
        combining is addition, joining is multiplication
      </div>
      <div style={box}>
        <div style={label}>a pair removed from both sides: new minus old</div>
        (<Weight w={1} /> <Weight w={-1} />) × (<Weight w={1} /> <Weight w={-1} />) − <Weight w={1} /> × <Weight w={1} /> = 0 − 1 = <Weight w={-1} />
        <div style={{ color: MUTED, fontSize: 11, marginTop: 2 }}>
          removed, even though <Weight w={-1} /> × <Weight w={-1} /> = <Weight w={1} /> on its own: that term corrects a double count
        </div>
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
