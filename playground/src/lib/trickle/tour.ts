'use client';

import { create } from 'zustand';
import type { Mode } from '../mode';
import { useTrickleStore, type TrickleState } from './store';

// The /trickle tour. Chapter 1 explains the ideas in cards over the canvas, each with a small figure.
// Chapter 2 watches the pipeline: each step runs it to a moment in a run and pauses there, and points at the
// nodes to look at. Copy lives here so it can be reviewed without touching components: `backticks` render as
// code, **double asterisks** as bold.

export type Figure =
  | { kind: 'weights' }
  | { kind: 'log' }
  | { kind: 'consolidate' }
  | { kind: 'join' }
  | { kind: 'rules' }
  | { kind: 'grid'; step: number }
  | { kind: 'both-deleted' }
  | { kind: 'legend' };

export interface TrickleStep {
  id: string;
  chapter: 1 | 2;
  intro?: boolean; // a chapter's opening card: just the chapter's title
  title: string;
  body: string | ((s: TrickleState) => string);
  figure?: Figure;
  ring?: string[]; // node keys, or 'events' for the sidebar's toggles
  enter?: (s: TrickleState) => void; // runs when the step opens
  // A link opens `href`, or with `mode` closes the tour and switches the Playground's mode.
  links?: { label: string; href?: string; mode?: Mode }[];
}

export const CHAPTERS: Record<1 | 2, string> = { 1: 'Theory', 2: 'Demonstration' };

const pct = (a: number, b: number) => `${Math.max(1, Math.round((100 * a) / Math.max(1, b)))}%`;

export const TRICKLE_STEPS: TrickleStep[] = [
  { id: 'intro-1', chapter: 1, intro: true, title: CHAPTERS[1], body: '' },
  {
    id: 'intro',
    chapter: 1,
    title: 'Trickle',
    body:
      '**Trickle** is the framework Duckstring uses to handle incremental processing. The goal is to reduce as much as possible the data read, computed and written with each pipeline run. The key concepts to understand are **Z-sets** as a change data format, and **freshness** as the natural epoch.',
  },
  {
    id: 'weights',
    chapter: 1,
    title: 'Z-sets',
    body:
      'You might be used to seeing change data feeds as records labelled "created", "updated" or "deleted". The Z-set format instead uses a weight field to represent creates as `+1`, deletes as `-1`, and updates as a delete of the previous record followed by a create of the new one.\n\nGrey rows here are existing records. All changes are appended.',
    figure: { kind: 'weights' },
  },
  {
    id: 'log',
    chapter: 1,
    title: 'Freshness',
    body:
      'From Duckstring’s orchestration we have a timestamp of **freshness**: the time the data entered the pipeline. This makes it simple to work out what has changed upstream since the last run.\n\nIn this example we’ve created, updated and then deleted product 42 over a few hours.',
    figure: { kind: 'log' },
  },
  {
    id: 'consolidate',
    chapter: 1,
    title: 'Consolidation',
    body:
      'Consolidating sums the weights of identical rows (apart from weight and freshness) and drops the rows that sum to zero. The two creates and two deletes in this window sum to zero, so the record for product 42 is dropped.',
    figure: { kind: 'consolidate' },
  },
  {
    id: 'join-pairs',
    chapter: 1,
    title: 'Joining Z-sets',
    body:
      'On joining, the weights from both sides are *multiplied* to produce the joined weight. The magic is that for an inner join this always works, even when joining unconsolidated tables with long histories, and even when both sides have a `-1`. You’ll see how next.',
    figure: { kind: 'join' },
  },
  {
    id: 'rules',
    chapter: 1,
    title: 'Table algebra',
    body:
      'On union, the weights are summed. On join, the weights are multiplied. Surprisingly, the result is that joins become distributive, so you can treat table unions as *addition* and table joins as *multiplication*, which is usually a trait of append-only tables.',
    figure: { kind: 'rules' },
  },
  {
    id: 'grid-0',
    chapter: 1,
    title: 'Incremental joins',
    body:
      'Say on the previous run `A ⋈ B` was computed. `A` and `B` have since updated to `A′` and `B′`, so `A′ ⋈ B′` needs evaluating. Using the product analogy, we can think of this as a square.',
    figure: { kind: 'grid', step: 0 },
  },
  {
    id: 'grid-1',
    chapter: 1,
    title: 'Separate changes',
    body:
      'Say `A′ = A + ΔA`, the union of its previous state and its changes. This gives four quadrants:\n\n`A′ ⋈ B′ = (A + ΔA) ⋈ (B + ΔB) = A ⋈ B + ΔA ⋈ B + A ⋈ ΔB + ΔA ⋈ ΔB`',
    figure: { kind: 'grid', step: 1 },
  },
  {
    id: 'grid-2',
    chapter: 1,
    title: 'Skip A ⋈ B',
    body:
      'We want to avoid as much as possible any calculation involving `A ⋈ B`, which was already calculated on the last run. Often this is by far the largest part of the calculation.',
    figure: { kind: 'grid', step: 2 },
  },
  {
    id: 'grid-3',
    chapter: 1,
    title: 'Consider changes only',
    body:
      'First consolidate `A′` and `B′`, and separately `ΔA` and `ΔB`. Then compute and union the two strips `ΔA ⋈ B′` and `A′ ⋈ ΔB`. The corner is counted twice, so also compute `ΔA ⋈ ΔB` and subtract it.',
    figure: { kind: 'grid', step: 3 },
  },
  {
    id: 'both-deleted',
    chapter: 1,
    title: 'Deleted on both sides',
    body:
      'Consider a special case where a delete (`-1`) is in both `ΔA` and `ΔB` for a given key. In `A′` and `B′` this consolidates to 0, so neither strip contains the record (or equivalently, each evaluates to 0). The sum is still `-1`, and the joined record is removed as expected.',
    figure: { kind: 'both-deleted' },
  },
  {
    id: 'deeper',
    chapter: 1,
    title: 'Deeper joins',
    body:
      'With a third table the algebra gets a lot more complex:\n\n`((A + ΔA) ⋈ (B + ΔB)) ⋈ (C + ΔC) = A ⋈ B ⋈ C + (A ⋈ B) ⋈ ΔC + …`\n\n`A ⋈ B ⋈ C` is the old result, skipped as before. But `(A ⋈ B) ⋈ ΔC` needs `A ⋈ B` in full, a recompute of the intermediate join, just to match a few changed rows of `C`. The other terms are partial mixtures of changes.\n\nFor each join, we can keep track of which keys appear in a change set, and pre-filter the other side of the join for just those keys, down to whichever source holds them. That bounds the computation to only what could have changed, however deep the joins go.',
  },
  { id: 'intro-2', chapter: 2, intro: true, title: CHAPTERS[2], body: '' },
  {
    id: 'pipeline',
    chapter: 2,
    title: 'Pipeline',
    body: 'On the first run, everything is new.',
    figure: { kind: 'legend' },
    ring: ['orders', 'catalog', 'priced', 'revenue'],
    enter: (s) => {
      s.reset();
      s.runTo(1, 'hold');
    },
  },
  {
    id: 'arrive',
    chapter: 2,
    title: 'Changes',
    body:
      'Run 2 brings 30 new `orders` lines (green) and 3 `catalog` changes (amber: an old row removed, a new one added). The grey rows are the state left by run 1.',
    ring: ['orders', 'catalog'],
    enter: (s) => s.runTo(2, 'sources'),
  },
  {
    id: 'join',
    chapter: 2,
    title: 'Computation',
    body: (s) => {
      const r = s.result;
      const took = r ? `${r.joinRows} rows, ${pct(r.joinRows, r.joinFull)} of the ${r.joinFull.toLocaleString()} a full recompute would join` : '';
      return `Only the cyan rows took part when producing \`priced\`: ${took}.`;
    },
    ring: ['priced', 'orders', 'catalog'],
    enter: (s) => s.runTo(2, 'join'),
  },
  {
    id: 'aggregate',
    chapter: 2,
    title: 'Aggregation',
    body: (s) => {
      const n = s.result ? s.result.revenue.updated + s.result.revenue.added : 0;
      return `In the last stage, ${n} products changed, and none of the others needed to be recomputed.`;
    },
    ring: ['revenue'],
    enter: (s) => s.runTo(2, 'hold'),
  },
  {
    id: 'fold',
    chapter: 2,
    title: 'Repeat',
    body: 'On the next generation all the changes are part of the previous state, allowing the process to cycle.',
    ring: ['orders', 'catalog', 'priced', 'revenue'],
    enter: (s) => s.runTo(3, 'sources'),
  },
  {
    id: 'try',
    chapter: 2,
    title: 'Try it',
    body:
      'You can play with this model yourself by adjusting what happens in each run. You can change the speed at the top left.',
    ring: ['events'],
    enter: (s) => {
      s.play();
      s.setSheetOpen(true);
    },
    links: [
      { label: 'Read the docs for Trickle', href: 'https://docs.duckstring.com/concepts/trickles' },
      { label: 'Orchestration tour →', mode: 'orchestration' },
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
