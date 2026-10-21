// Z-sets: rows with an integer weight (+1 present, -1 removed). Combining changes adds weights and a join
// multiplies them, which is what lets a join's output be updated from its inputs' changes. A small model of
// the Trickle engine (packages/trickle in the Duckstring repo), enough to drive the /trickle page.

export interface Weighted<R> {
  row: R;
  w: number;
}

export type ZSet<R> = Weighted<R>[];

// The current state of a table as a Z-set: every row weighs 1.
export function asZSet<R>(rows: Iterable<R>): ZSet<R> {
  return [...rows].map((row) => ({ row, w: 1 }));
}

// Sum the weights of identical rows (by `ident`) and drop rows whose weights cancel.
export function consolidate<R>(z: ZSet<R>, ident: (r: R) => string): ZSet<R> {
  const sums = new Map<string, Weighted<R>>();
  for (const { row, w } of z) {
    const k = ident(row);
    const cur = sums.get(k);
    if (cur) cur.w += w;
    else sums.set(k, { row, w });
  }
  return [...sums.values()].filter((x) => x.w !== 0);
}

// An equi-join of two Z-sets: each pair of rows with equal keys gives one row weighing the product of theirs.
export function join<L, R, O>(
  left: ZSet<L>,
  right: ZSet<R>,
  leftKey: (l: L) => number,
  rightKey: (r: R) => number,
  combine: (l: L, r: R) => O,
): ZSet<O> {
  const index = new Map<number, Weighted<R>[]>();
  for (const r of right) {
    const k = rightKey(r.row);
    const list = index.get(k);
    if (list) list.push(r);
    else index.set(k, [r]);
  }
  const out: ZSet<O> = [];
  for (const l of left) {
    for (const r of index.get(leftKey(l.row)) ?? []) out.push({ row: combine(l.row, r.row), w: l.w * r.w });
  }
  return out;
}

export function scale<R>(z: ZSet<R>, by: number): ZSet<R> {
  return z.map(({ row, w }) => ({ row, w: w * by }));
}

// δ(A ⋈ B) = δA ⋈ B' + A' ⋈ δB − δA ⋈ δB, with A' and B' the states after the change. The engine's rule for an
// inner join: no earlier state is needed, and an empty change contributes no terms.
export function joinDelta<L, R, O>(
  dA: ZSet<L>,
  aNew: ZSet<L>,
  dB: ZSet<R>,
  bNew: ZSet<R>,
  leftKey: (l: L) => number,
  rightKey: (r: R) => number,
  combine: (l: L, r: R) => O,
): ZSet<O> {
  return [
    ...join(dA, bNew, leftKey, rightKey, combine),
    ...join(aNew, dB, leftKey, rightKey, combine),
    ...scale(join(dA, dB, leftKey, rightKey, combine), -1),
  ];
}
