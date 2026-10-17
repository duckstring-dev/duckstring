// The Trickle demo pipeline at a size where every row fits on screen: `orders` (an append-only Trickle of order
// lines) and `catalog` (a merge Trickle of product prices) feed `priced` (their join), which feeds `revenue`
// (totals per product). Each run computes the change to `priced` from the sources' changes with the engine's
// join rule, and the change to `revenue` from `priced`'s change, then applies them. Nothing is recomputed in
// full after the first run; tests/unit/trickle.spec.ts checks the result against a full recompute.

import { asZSet, consolidate, joinDelta, type ZSet } from './zset';

export interface Order {
  id: number;
  product: number;
  qty: number;
}

export interface Product {
  id: number;
  price: number; // in cents
}

export interface Priced {
  order: number;
  product: number;
  qty: number;
  price: number;
  revenue: number;
}

export interface Revenue {
  product: number;
  revenue: number;
  units: number;
  orders: number;
}

// What a run did to one row, for drawing: `read` took part in the computation without changing.
export type Mark = 'base' | 'read' | 'added' | 'updated' | 'removed';

export interface TableView {
  ids: number[]; // display order; removed rows stay until the next run folds them away
  marks: Map<number, Mark>;
  added: number;
  updated: number;
  removed: number;
  read: number;
  size: number; // rows in the table after the run
}

export interface RunResult {
  run: number;
  bootstrap: boolean;
  orders: TableView;
  catalog: TableView;
  priced: TableView;
  revenue: TableView;
  joinRows: number; // rows of orders and catalog that took part in computing priced
  joinFull: number; // rows a full recompute of priced would join
  aggRows: number; // priced changes plus revenue totals read to update revenue
  aggFull: number; // rows a full recompute of revenue would read
}

export interface RunEvents {
  newOrders: boolean;
  reprice: boolean;
  discontinue: boolean;
}

export interface SimSize {
  products: number;
  orders: number; // at the first run
  batch: number; // new order lines per run
  repriced: number; // products whose price changes per run
}

export const DEFAULT_SIZE: SimSize = { products: 200, orders: 2000, batch: 40, repriced: 4 };

const pricedIdent = (p: Priced) => `${p.order}:${p.product}:${p.qty}:${p.price}`;
const priceOf = (o: Order, p: Product): Priced => ({
  order: o.id,
  product: p.id,
  qty: o.qty,
  price: p.price,
  revenue: o.qty * p.price,
});

// A small seeded generator, so a page load and a test see the same runs.
function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class TrickleSim {
  readonly size: SimSize;
  orders = new Map<number, Order>();
  catalog = new Map<number, Product>();
  priced = new Map<number, Priced>(); // by order id
  revenue = new Map<number, Revenue>(); // by product id
  run = 0;
  private rand: () => number;
  private nextOrder = 1;
  private shown: Record<'orders' | 'catalog' | 'priced' | 'revenue', number[]> = {
    orders: [], catalog: [], priced: [], revenue: [],
  };

  constructor(seed = 7, size: SimSize = DEFAULT_SIZE) {
    this.size = size;
    this.rand = mulberry32(seed);
  }

  private pick<T>(xs: T[]): T {
    return xs[Math.floor(this.rand() * xs.length)];
  }

  private newOrders(n: number): Order[] {
    const products = [...this.catalog.keys()];
    if (products.length === 0) return [];
    return Array.from({ length: n }, () => ({
      id: this.nextOrder++,
      product: this.pick(products),
      qty: 1 + Math.floor(this.rand() * 5),
    }));
  }

  // One run: the sources change (all of them on the first run), then priced and revenue follow.
  step(events: RunEvents = { newOrders: true, reprice: true, discontinue: false }): RunResult {
    this.run += 1;
    const bootstrap = this.run === 1;
    const dOrders: ZSet<Order> = [];
    const dCatalog: ZSet<Product> = [];

    if (bootstrap) {
      for (let id = 1; id <= this.size.products; id++) {
        dCatalog.push({ row: { id, price: 500 + Math.floor(this.rand() * 40) * 50 }, w: 1 });
      }
      for (const { row } of dCatalog) this.catalog.set(row.id, row);
      for (const o of this.newOrders(this.size.orders)) dOrders.push({ row: o, w: 1 });
    } else {
      if (events.reprice) {
        const ids = [...this.catalog.keys()];
        const chosen = new Set<number>();
        while (chosen.size < Math.min(this.size.repriced, ids.length)) chosen.add(this.pick(ids));
        for (const id of chosen) {
          const old = this.catalog.get(id)!;
          const factor = this.pick([0.85, 0.9, 1.1, 1.2]);
          const now = { id, price: Math.max(50, Math.round((old.price * factor) / 10) * 10) };
          if (now.price === old.price) continue;
          dCatalog.push({ row: old, w: -1 }, { row: now, w: 1 });
        }
      }
      if (events.discontinue && this.catalog.size > 1) {
        // A product that has been ordered, so the removal reaches priced and revenue.
        const ordered = [...this.revenue.keys()].filter((id) => !dCatalog.some((d) => d.row.id === id));
        if (ordered.length) {
          const id = this.pick(ordered);
          dCatalog.push({ row: this.catalog.get(id)!, w: -1 });
        }
      }
      for (const d of dCatalog) {
        if (d.w > 0) this.catalog.set(d.row.id, d.row);
        else if (!dCatalog.some((e) => e.w > 0 && e.row.id === d.row.id)) this.catalog.delete(d.row.id);
      }
      if (events.newOrders) for (const o of this.newOrders(this.size.batch)) dOrders.push({ row: o, w: 1 });
    }
    for (const { row } of dOrders) this.orders.set(row.id, row);

    // priced: the change from the sources' changes, against their states after the change.
    const ordersNow = asZSet(this.orders.values());
    const catalogNow = asZSet(this.catalog.values());
    const dPriced = consolidate(
      joinDelta(dOrders, ordersNow, dCatalog, catalogNow, (o) => o.product, (p) => p.id, priceOf),
      pricedIdent,
    );
    for (const d of dPriced) if (d.w < 0 && this.priced.get(d.row.order)?.price === d.row.price) this.priced.delete(d.row.order);
    for (const d of dPriced) if (d.w > 0) this.priced.set(d.row.order, d.row);

    // revenue: fold each priced change into its product's totals.
    const touched = new Map<number, Revenue | null>(); // product → its totals before the run (null: new)
    for (const { row, w } of dPriced) {
      if (!touched.has(row.product)) {
        const cur = this.revenue.get(row.product);
        touched.set(row.product, cur ? { ...cur } : null);
      }
      const cur = this.revenue.get(row.product) ?? { product: row.product, revenue: 0, units: 0, orders: 0 };
      const next = { ...cur, revenue: cur.revenue + w * row.revenue, units: cur.units + w * row.qty, orders: cur.orders + w };
      if (next.orders === 0) this.revenue.delete(row.product);
      else this.revenue.set(row.product, next);
    }

    // What each table shows: keys of the other side's change say which current rows took part in the join.
    const catalogKeys = new Set(dCatalog.map((d) => d.row.id));
    const orderKeys = new Set(dOrders.map((d) => d.row.product));
    const ordersView = this.view('orders', [...this.orders.keys()], (id) => {
      if (dOrders.some((d) => d.row.id === id)) return 'added';
      return !bootstrap && catalogKeys.has(this.orders.get(id)!.product) ? 'read' : 'base';
    });
    const catalogChange = changeMarks(dCatalog.map((d) => ({ id: d.row.id, w: d.w })));
    const catalogView = this.view('catalog', [...this.catalog.keys()], (id) => {
      const m = catalogChange.get(id);
      if (m) return m;
      return !bootstrap && orderKeys.has(id) ? 'read' : 'base';
    }, catalogChange);
    const pricedChange = changeMarks(dPriced.map((d) => ({ id: d.row.order, w: d.w })));
    const pricedView = this.view('priced', [...this.priced.keys()], (id) => pricedChange.get(id) ?? 'base', pricedChange);
    const revenueChange = new Map<number, Mark>();
    for (const [product, before] of touched) {
      const after = this.revenue.get(product);
      revenueChange.set(product, !before ? 'added' : after ? 'updated' : 'removed');
    }
    const revenueView = this.view('revenue', [...this.revenue.keys()], (id) => revenueChange.get(id) ?? 'base', revenueChange);

    const joinRows = bootstrap
      ? this.orders.size + this.catalog.size
      : dOrders.length + dCatalog.length + ordersView.read + catalogView.read;
    return {
      run: this.run,
      bootstrap,
      orders: ordersView,
      catalog: catalogView,
      priced: pricedView,
      revenue: revenueView,
      joinRows,
      joinFull: this.orders.size + this.catalog.size,
      aggRows: bootstrap ? this.priced.size : dPriced.length + [...touched.values()].filter(Boolean).length,
      aggFull: this.priced.size,
    };
  }

  // The rows to draw, in a stable order: the previous run's rows (removed ones folded away), then new ones.
  private view(
    table: 'orders' | 'catalog' | 'priced' | 'revenue',
    current: number[],
    mark: (id: number) => Mark,
    change?: Map<number, Mark>,
  ): TableView {
    const present = new Set(current);
    const kept = this.shown[table].filter((id) => present.has(id) || change?.get(id) === 'removed');
    const keptSet = new Set(kept);
    const ids = [...kept, ...current.filter((id) => !keptSet.has(id)).sort((a, b) => a - b)];
    for (const [id, m] of change ?? []) if (m === 'removed' && !keptSet.has(id) && !present.has(id)) ids.push(id);
    const marks = new Map<number, Mark>();
    const counts = { added: 0, updated: 0, removed: 0, read: 0 };
    for (const id of ids) {
      const m = present.has(id) ? mark(id) : 'removed';
      marks.set(id, m);
      if (m !== 'base') counts[m] += 1;
    }
    this.shown[table] = ids.filter((id) => present.has(id));
    return { ids, marks, ...counts, size: current.length };
  }
}

// Per id, what a change did: only added, only removed, or both (an update).
function changeMarks(changes: { id: number; w: number }[]): Map<number, Mark> {
  const seen = new Map<number, { add: boolean; remove: boolean }>();
  for (const { id, w } of changes) {
    const s = seen.get(id) ?? { add: false, remove: false };
    if (w > 0) s.add = true;
    else s.remove = true;
    seen.set(id, s);
  }
  const out = new Map<number, Mark>();
  for (const [id, s] of seen) out.set(id, s.add && s.remove ? 'updated' : s.add ? 'added' : 'removed');
  return out;
}

// A full recompute of priced and revenue from the current sources, for checking the incremental result.
export function recompute(sim: TrickleSim): { priced: Map<number, Priced>; revenue: Map<number, Revenue> } {
  const priced = new Map<number, Priced>();
  for (const o of sim.orders.values()) {
    const p = sim.catalog.get(o.product);
    if (p) priced.set(o.id, priceOf(o, p));
  }
  const revenue = new Map<number, Revenue>();
  for (const p of priced.values()) {
    const cur = revenue.get(p.product) ?? { product: p.product, revenue: 0, units: 0, orders: 0 };
    revenue.set(p.product, { ...cur, revenue: cur.revenue + p.revenue, units: cur.units + p.qty, orders: cur.orders + 1 });
  }
  return { priced, revenue };
}

