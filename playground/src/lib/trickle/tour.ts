'use client';

import { create } from 'zustand';
import { useTrickleStore, type TrickleState } from './store';

// The /trickle tour. Chapter 1 explains the ideas in cards over the canvas, each with a small figure.
// Chapter 2 watches the pipeline: each step runs it to a moment in a run and pauses there, and points at the
// nodes to look at. Copy lives here so it can be reviewed without touching components: `backticks` render as
// code, **double asterisks** as bold.

export type Figure =
  | { kind: 'weights' }
  | { kind: 'log' }
  | { kind: 'consolidate' }
  | { kind: 'join-log' }
  | { kind: 'rules' }
  | { kind: 'grid'; step: number }
  | { kind: 'keys'; narrow: boolean };

export interface TrickleStep {
  id: string;
  chapter: 1 | 2;
  title: string;
  body: string | ((s: TrickleState) => string);
  figure?: Figure;
  ring?: string[]; // node keys, or 'events' for the sidebar's toggles
  enter?: (s: TrickleState) => void; // runs when the step opens
  links?: { label: string; href: string }[];
}

export const CHAPTERS: Record<1 | 2, string> = { 1: 'How it works', 2: 'Watch it run' };

const pct = (a: number, b: number) => `${Math.max(1, Math.round((100 * a) / Math.max(1, b)))}%`;

export const TRICKLE_STEPS: TrickleStep[] = [
  {
    id: 'intro',
    chapter: 1,
    title: 'Trickle',
    body:
      'Incremental processing updates a result from what changed, instead of recomputing it. A **Trickle** is how Duckstring does it: changes are stored as **Z-sets**, and each is stamped with the **freshness** of the run that made it. A few cards on how that works, then a pipeline to watch.',
  },
  {
    id: 'weights',
    chapter: 1,
    title: 'A Z-set: rows with a weight',
    body:
      'Each change is the whole row with a weight: `+1` for a row added, `-1` for a row removed. An update is both, the old row removed and the new one added. Grey rows are already in `catalog`.',
    figure: { kind: 'weights' },
  },
  {
    id: 'log',
    chapter: 1,
    title: 'Each change has a freshness',
    body:
      'Changes are appended, never applied in place, and each is stamped with the freshness of its run: a timestamp that works as an epoch. Here product 42 is added at 09:00, repriced at 10:00 and removed at 11:00. A run reads the changes since its previous run’s freshness.',
    figure: { kind: 'log' },
  },
  {
    id: 'consolidate',
    chapter: 1,
    title: 'Consolidation',
    body:
      'Summing the weights of identical rows gives the table as of any freshness. A row whose weights sum to zero is gone: up to 10:00 the 10.00 rows cancel, and by 11:00 everything for product 42 does.',
    figure: { kind: 'consolidate' },
  },
  {
    id: 'join-log',
    chapter: 1,
    title: 'Joining changes',
    body:
      'Join an order line for product 42 to those same changes. Each joined row weighs the product of its two weights, so `priced` gets the matching changes: the 10.00 line added at 09:00, swapped for 12.00 at 10:00, and removed at 11:00.',
    figure: { kind: 'join-log' },
  },
  {
    id: 'rules',
    chapter: 1,
    title: 'Addition and multiplication',
    body:
      'So combining Z-sets adds weights, and joining them multiplies weights, for inserts, updates and deletes alike. That is what lets a join’s result be updated from its changes alone.',
    figure: { kind: 'rules' },
  },
  {
    id: 'grid-0',
    chapter: 1,
    title: 'A join, one run later',
    body:
      'After a run each table is its previous state plus its changes: `A′ = A + ΔA`. Here `A` is the order lines and `B` the catalog. The part made only of previous states, `A ⋈ B`, is last run’s output, which is already stored.',
    figure: { kind: 'grid', step: 0 },
  },
  {
    id: 'grid-1',
    chapter: 1,
    title: 'The new order lines',
    body: 'Join the new order lines, `ΔA`, to the whole catalog as it is now, `B′`.',
    figure: { kind: 'grid', step: 1 },
  },
  {
    id: 'grid-2',
    chapter: 1,
    title: 'The catalog changes',
    body: 'Join all the order lines as they are now, `A′`, to the catalog changes, `ΔB`. The corner, `ΔA ⋈ ΔB`, is now counted twice.',
    figure: { kind: 'grid', step: 2 },
  },
  {
    id: 'grid-3',
    chapter: 1,
    title: 'Subtract the corner',
    body:
      'Take the corner away once, and what is left is exactly the change to the output. Every term uses the tables as they are now, so no earlier version of a table has to be rebuilt.',
    figure: { kind: 'grid', step: 3 },
  },
  {
    id: 'keys-whole',
    chapter: 1,
    title: 'Each strip spans a whole table',
    body: 'As drawn, `A′ ⋈ ΔB` covers every order line, and `ΔA ⋈ B′` every product.',
    figure: { kind: 'keys', narrow: false },
  },
  {
    id: 'keys-narrow',
    chapter: 1,
    title: 'Only matching keys take part',
    body:
      'A join only pairs rows with equal keys: just the order lines for the repriced products, and the products the new order lines name. Semi, anti and outer joins narrow the same way.',
    figure: { kind: 'keys', narrow: true },
  },
  {
    id: 'pipeline',
    chapter: 2,
    title: 'The pipeline',
    body:
      '`orders` and `catalog` feed `priced`, their join, which feeds `revenue`, the totals per product. Every square is a row. The first run has nothing earlier to build on, so every row is new and everything is computed.',
    ring: ['orders', 'catalog', 'priced', 'revenue'],
    enter: (s) => {
      s.reset();
      s.runTo(1, 'hold');
    },
  },
  {
    id: 'arrive',
    chapter: 2,
    title: 'New rows arrive',
    body:
      'Run 2 brings 30 new order lines (green) and 3 price changes (amber: an old row removed, a new one added). The grey rows are the state left by run 1.',
    ring: ['orders', 'catalog'],
    enter: (s) => s.runTo(2, 'sources'),
  },
  {
    id: 'join',
    chapter: 2,
    title: 'priced works from the changes',
    body: (s) => {
      const r = s.result;
      const took = r ? `${r.joinRows} rows, ${pct(r.joinRows, r.joinFull)} of the ${r.joinFull.toLocaleString()} a full recompute would join` : '';
      return `\`priced\` joins the new order lines to the catalog, and the catalog changes to the order lines. Only the cyan rows took part: ${took}.`;
    },
    ring: ['priced', 'orders', 'catalog'],
    enter: (s) => s.runTo(2, 'join'),
  },
  {
    id: 'aggregate',
    chapter: 2,
    title: 'revenue updates its totals',
    body: (s) => {
      const n = s.result ? s.result.revenue.updated + s.result.revenue.added : 0;
      return `Each change to \`priced\` is added to its product's totals. ${n} products changed, and every other total was left alone, unread.`;
    },
    ring: ['revenue'],
    enter: (s) => s.runTo(2, 'hold'),
  },
  {
    id: 'fold',
    chapter: 2,
    title: 'Fold, and go again',
    body: 'Before each run, the last run’s changes become part of the state, and the next changes arrive. A run only ever touches its own changes.',
    ring: ['orders', 'catalog', 'priced', 'revenue'],
    enter: (s) => s.runTo(3, 'sources'),
  },
  {
    id: 'try',
    chapter: 2,
    title: 'Your turn',
    body:
      'Turn on **Remove a product** to see a deletion flow through: its order lines leave `priced` and its total leaves `revenue`, in red. Pause and speed are at the top left, and **Orchestration** at the top of the sidebar shows when Ponds run.',
    ring: ['events'],
    enter: (s) => {
      s.play();
      s.setSheetOpen(true);
    },
    links: [
      { label: 'Trickles in the docs', href: 'https://docs.duckstring.com/concepts/trickles' },
    ],
  },
];

const STORAGE_KEY = 'ds-trickle-tour';

export function rememberTourDone() {
  try {
    window.localStorage.setItem(STORAGE_KEY, 'done');
  } catch {
    // Blocked storage: the tour just offers itself again next time.
  }
}

export function tourWasDone(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'done';
  } catch {
    return false;
  }
}

interface TourState {
  open: boolean;
  index: number;
  inset: { top: number; bottom: number }; // the canvas the card covers, for framing the graph around it
  setInset(inset: { top: number; bottom: number }): void;
  start(): void;
  go(index: number): void;
  next(): void;
  back(): void;
  close(): void;
}

function openStep(index: number) {
  const step = TRICKLE_STEPS[index];
  const s = useTrickleStore.getState();
  s.setRing(step.ring ?? []);
  if (step.chapter === 1) {
    if (s.phase !== 'empty') s.reset();
  } else {
    step.enter?.(useTrickleStore.getState());
  }
}

export const useTrickleTour = create<TourState>((set, get) => ({
  open: false,
  index: 0,
  inset: { top: 0, bottom: 0 },
  setInset(inset) {
    const cur = get().inset;
    if (cur.top !== inset.top || cur.bottom !== inset.bottom) set({ inset });
  },
  start() {
    set({ open: true, index: 0 });
    openStep(0);
  },
  go(index) {
    set({ open: true, index });
    openStep(index);
  },
  next() {
    const i = get().index;
    if (i >= TRICKLE_STEPS.length - 1) return get().close();
    get().go(i + 1);
  },
  back() {
    const i = get().index;
    if (i > 0) get().go(i - 1);
  },
  close() {
    set({ open: false, inset: { top: 0, bottom: 0 } });
    rememberTourDone();
    const s = useTrickleStore.getState();
    s.setRing([]);
    s.play();
  },
}));
