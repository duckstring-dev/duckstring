'use client';

import { create } from 'zustand';
import { useTrickleStore, type TrickleState } from './store';

// The /trickle tour. Chapter 1 explains the ideas in cards over the canvas, each with a small figure.
// Chapter 2 watches the pipeline: each step runs it to a moment in a run and pauses there, and points at the
// nodes to look at. Copy lives here so it can be reviewed without touching components: `backticks` render as
// code, **double asterisks** as bold.

export type Figure =
  | { kind: 'weights' }
  | { kind: 'combine' }
  | { kind: 'multiply' }
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
      'A step in a pipeline normally rewrites its tables in full on every run, even when only a few rows changed. A **Trickle** is a table that keeps a record of its changes, so the tables built from it can be updated from just those changes. A few ideas first, then a pipeline to watch.',
  },
  {
    id: 'weights',
    chapter: 1,
    title: 'Changes are rows with a weight',
    body:
      'Each change is the whole row with a weight: `+1` for a row added, `-1` for a row removed. An update is both. A set of rows with weights is called a **Z-set**.',
    figure: { kind: 'weights' },
  },
  {
    id: 'combine',
    chapter: 1,
    title: 'Combining changes is addition',
    body: 'The weights of identical rows add up, and a row whose weights sum to zero no longer exists.',
    figure: { kind: 'combine' },
  },
  {
    id: 'multiply',
    chapter: 1,
    title: 'Joining is multiplication',
    body:
      'A joined row weighs the product of the weights it came from. `+1 × -1` gives `-1`, so the old priced line is removed and the new one added, with no special handling for updates or deletes.',
    figure: { kind: 'multiply' },
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
