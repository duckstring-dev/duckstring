'use client';

import { CellGrid } from './CellGrid';
import { FAINT, MONO, MUTED, TEXT, type CellMark } from './theme';

// The two strips of the join, narrowed by key. A join only pairs rows with equal keys, so A′ ⋈ ΔB needs only
// the order lines for the products in ΔB, and ΔA ⋈ B′ only the products the new order lines name.

const PRODUCTS = 60;
const ORDERS = 480;
const NEW_ORDERS = 12;
const REPRICED = [7, 23, 41];

function data() {
  let seed = 11;
  const rand = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;
  const product = Array.from({ length: ORDERS + NEW_ORDERS }, () => 1 + Math.floor(rand() * PRODUCTS));
  const named = new Set(product.slice(ORDERS));
  return { product, named };
}

const DATA = data();

export function KeySet({ narrow }: { narrow: boolean }) {
  const { product, named } = DATA;
  const repriced = new Set(REPRICED);

  const orderMarks: CellMark[] = product.map((p) => (!narrow || repriced.has(p) ? 'read' : 'dim'));
  const productMarks: CellMark[] = Array.from({ length: PRODUCTS }, (_, i) => (!narrow || named.has(i + 1) ? 'read' : 'dim'));
  const ordersIn = orderMarks.filter((m) => m === 'read').length;
  const productsIn = productMarks.filter((m) => m === 'read').length;

  return (
    <div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div>
          <div style={label}>A′ ⋈ ΔB</div>
          <div style={sub}>order lines, joined to {REPRICED.length} repriced products</div>
          <CellGrid marks={orderMarks} label={`${ordersIn} of ${product.length} order lines take part`} cell={5} gap={2} />
          <div data-testid="keys-orders" style={count}>
            {ordersIn} of {product.length} order lines take part
          </div>
        </div>
        <div>
          <div style={label}>ΔA ⋈ B′</div>
          <div style={sub}>products, joined to {NEW_ORDERS} new order lines</div>
          <CellGrid marks={productMarks} label={`${productsIn} of ${PRODUCTS} products take part`} cell={5} gap={2} />
          <div style={count}>
            {productsIn} of {PRODUCTS} products take part
          </div>
        </div>
      </div>
    </div>
  );
}

const label: React.CSSProperties = { fontFamily: MONO, fontSize: 13, color: TEXT };
const sub: React.CSSProperties = { fontSize: 12, color: FAINT, margin: '2px 0 8px' };
const count: React.CSSProperties = { fontFamily: MONO, fontSize: 12, color: MUTED, marginTop: 8 };
