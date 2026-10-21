'use client';

import { create } from 'zustand';
import { TrickleSim, type RunEvents, type RunResult, type SimSize, type TableView } from './pipeline';

// The /trickle page's state: the pipeline, and where the current run has got to. A run plays out in phases,
// so each step can be seen: the sources change, priced joins the changes, revenue updates its totals, the
// result holds on screen, then folds into the state before the next run.

export type Phase = 'empty' | 'sources' | 'join' | 'aggregate' | 'hold' | 'fold';
export type PondKey = 'orders' | 'catalog' | 'priced' | 'revenue';

// Simulated milliseconds per phase at 1× speed.
export const PHASE_MS: Record<Exclude<Phase, 'empty'>, number> = {
  sources: 1500,
  join: 1900,
  aggregate: 1600,
  hold: 2400,
  fold: 800,
};

const NEXT: Record<Exclude<Phase, 'empty'>, Exclude<Phase, 'empty'>> = {
  sources: 'join',
  join: 'aggregate',
  aggregate: 'hold',
  hold: 'fold',
  fold: 'sources',
};

export const SIZE: SimSize = { products: 120, orders: 900, batch: 30, repriced: 3 };

export interface TrickleState {
  sim: TrickleSim;
  result: RunResult | null;
  phase: Phase;
  elapsed: number; // in the phase, simulated ms
  paused: boolean;
  speed: number;
  events: RunEvents;
  stopAt: { run: number; phase: Phase } | null; // pause when the pipeline gets here
  ring: string[]; // what the tour is pointing at (node keys, or 'events')
  sheetOpen: boolean; // the phone's bottom sheet
  tick(dtMs: number): void;
  play(): void;
  togglePause(): void;
  setSpeed(speed: number): void;
  toggleEvent(key: keyof RunEvents): void;
  reset(): void;
  runTo(run: number, phase: Phase): void;
  setRing(ring: string[]): void;
  setSheetOpen(open: boolean): void;
}

const DEFAULT_EVENTS: RunEvents = { newOrders: true, reprice: true, discontinue: false };

export const useTrickleStore = create<TrickleState>((set, get) => {
  // Enter `phase`, running the next pipeline step when a new run starts, and stopping if asked to.
  const enter = (phase: Exclude<Phase, 'empty'>) => {
    const s = get();
    let result = s.result;
    if (phase === 'sources') result = s.sim.step(s.events);
    const stop = s.stopAt && result && s.stopAt.run === result.run && s.stopAt.phase === phase;
    set({ phase, elapsed: 0, result, ...(stop ? { paused: true, stopAt: null } : {}) });
  };

  return {
    sim: new TrickleSim(7, SIZE),
    result: null,
    phase: 'empty',
    elapsed: 0,
    paused: true,
    speed: 1,
    events: DEFAULT_EVENTS,
    stopAt: null,
    ring: [],
    sheetOpen: false,

    tick(dtMs) {
      const s = get();
      if (s.paused) return;
      if (s.phase === 'empty') return enter('sources');
      const elapsed = s.elapsed + dtMs * s.speed;
      if (elapsed < PHASE_MS[s.phase]) return set({ elapsed });
      enter(NEXT[s.phase]);
    },
    play() {
      set({ paused: false });
    },
    togglePause() {
      set((s) => ({ paused: !s.paused, stopAt: null }));
    },
    setSpeed(speed) {
      set({ speed });
    },
    toggleEvent(key) {
      set((s) => ({ events: { ...s.events, [key]: !s.events[key] } }));
    },
    reset() {
      set({ sim: new TrickleSim(7, SIZE), result: null, phase: 'empty', elapsed: 0, paused: true, stopAt: null, events: DEFAULT_EVENTS });
    },
    // Run on until `run` reaches `phase`, then pause. Already there (or past it): pause where it is.
    runTo(run, phase) {
      const s = get();
      const order: Phase[] = ['empty', 'sources', 'join', 'aggregate', 'hold', 'fold'];
      const at = s.result?.run ?? 0;
      const past = at > run || (at === run && order.indexOf(s.phase) >= order.indexOf(phase));
      if (past) return set({ paused: true, stopAt: null });
      set({ stopAt: { run, phase }, paused: false });
    },
    setRing(ring) {
      set({ ring });
    },
    setSheetOpen(sheetOpen) {
      set({ sheetOpen });
    },
  };
});

// ─── What each node shows in each phase ─────────────────────────────────────────

export type Stage = 'before' | 'change' | 'full' | 'folded';

// How far through the run each table is: a downstream table shows its previous state until its own phase.
export function stageOf(pond: PondKey, phase: Phase): Stage {
  if (phase === 'fold') return 'folded';
  const source = pond === 'orders' || pond === 'catalog';
  if (source) return phase === 'sources' ? 'change' : 'full';
  if (pond === 'priced') return phase === 'sources' ? 'before' : 'full';
  return phase === 'sources' || phase === 'join' ? 'before' : 'full';
}

export function stagedMarks(view: TableView, stage: Stage): import('./pipeline').Mark[] {
  const m = (id: number) => view.marks.get(id) ?? 'base';
  switch (stage) {
    case 'before':
      return view.ids.filter((id) => m(id) !== 'added').map(() => 'base');
    case 'change':
      return view.ids.map((id) => (m(id) === 'read' ? 'base' : m(id)));
    case 'full':
      return view.ids.map(m);
    case 'folded':
      return view.ids.filter((id) => m(id) !== 'removed').map(() => 'base');
  }
}

// The rows in a table's change this run: an update is two (the old row out, the new one in).
export function changeRows(view: TableView): number {
  return view.added + view.removed + 2 * view.updated;
}
