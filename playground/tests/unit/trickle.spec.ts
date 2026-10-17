import { test, expect } from '@playwright/test';
import { TrickleSim, recompute, type RunEvents } from '../../src/lib/trickle/pipeline';
import { asZSet, consolidate, join, joinDelta, type ZSet } from '../../src/lib/trickle/zset';

// The /trickle page's pipeline is computed incrementally. After every run, priced and revenue must equal a
// full recompute from the sources, whatever mix of changes the run had.

const sorted = <T,>(m: Map<number, T>) => [...m.entries()].sort((a, b) => a[0] - b[0]);

test('the incremental pipeline matches a full recompute after every run', () => {
  for (let seed = 1; seed <= 20; seed++) {
    const sim = new TrickleSim(seed, { products: 30, orders: 200, batch: 15, repriced: 3 });
    for (let run = 0; run < 12; run++) {
      const r = (seed * 31 + run * 7) % 8;
      const events: RunEvents = { newOrders: (r & 1) === 1, reprice: (r & 2) === 2, discontinue: (r & 4) === 4 };
      sim.step(events);
      const truth = recompute(sim);
      expect(sorted(sim.priced), `seed ${seed}, run ${run}`).toEqual(sorted(truth.priced));
      expect(sorted(sim.revenue), `seed ${seed}, run ${run}`).toEqual(sorted(truth.revenue));
    }
  }
});

test('the join delta equals the new join minus the old one, for any changes on both sides', () => {
  type A = { id: number; k: number };
  type B = { id: number; k: number; v: number };
  const ident = (r: { a: number; b: number; v: number }) => `${r.a}:${r.b}:${r.v}`;
  const combine = (a: A, b: B) => ({ a: a.id, b: b.id, v: b.v });
  const aOld: A[] = [{ id: 1, k: 1 }, { id: 2, k: 1 }, { id: 3, k: 2 }];
  const bOld: B[] = [{ id: 1, k: 1, v: 10 }, { id: 2, k: 2, v: 20 }, { id: 3, k: 2, v: 21 }];
  const dA: ZSet<A> = [{ row: { id: 2, k: 1 }, w: -1 }, { row: { id: 4, k: 2 }, w: 1 }];
  const dB: ZSet<B> = [{ row: { id: 2, k: 2, v: 20 }, w: -1 }, { row: { id: 2, k: 2, v: 25 }, w: 1 }, { row: { id: 4, k: 1, v: 5 }, w: 1 }];
  const aNew = [{ id: 1, k: 1 }, { id: 3, k: 2 }, { id: 4, k: 2 }];
  const bNew = [{ id: 1, k: 1, v: 10 }, { id: 2, k: 2, v: 25 }, { id: 3, k: 2, v: 21 }, { id: 4, k: 1, v: 5 }];
  const delta = consolidate(joinDelta(dA, asZSet(aNew), dB, asZSet(bNew), (a) => a.k, (b) => b.k, combine), ident);
  const before = join(asZSet(aOld), asZSet(bOld), (a) => a.k, (b) => b.k, combine);
  const after = join(asZSet(aNew), asZSet(bNew), (a) => a.k, (b) => b.k, combine);
  const truth = consolidate([...after, ...before.map((x) => ({ ...x, w: -x.w }))], ident);
  const key = (z: ZSet<{ a: number; b: number; v: number }>) => z.map((x) => `${ident(x.row)}=${x.w}`).sort();
  expect(key(delta)).toEqual(key(truth));
});

test('after the first run, only a small part of the tables takes part', () => {
  const sim = new TrickleSim();
  const first = sim.step();
  expect(first.bootstrap).toBe(true);
  expect(first.joinRows).toBe(first.joinFull);
  const second = sim.step({ newOrders: true, reprice: true, discontinue: false });
  expect(second.orders.added).toBe(40);
  expect(second.catalog.updated).toBeGreaterThan(0);
  expect(second.joinRows).toBeLessThan(second.joinFull / 5);
  // A row removed in one run is drawn that run and folded away by the next.
  const third = sim.step({ newOrders: false, reprice: false, discontinue: true });
  const removed = third.priced.ids.filter((id) => third.priced.marks.get(id) === 'removed');
  expect(removed.length).toBeGreaterThan(0);
  const fourth = sim.step({ newOrders: true, reprice: false, discontinue: false });
  expect(removed.some((id) => fourth.priced.ids.includes(id))).toBe(false);
});
