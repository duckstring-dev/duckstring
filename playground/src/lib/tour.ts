import { create } from 'zustand';
import { track } from '@vercel/analytics';
import { usePlaygroundStore } from './store';
import { writeTourMemory } from './tourStorage';
import { TOUR_STEPS, pondByName, rippleByName, type TourControl, type TourStep, type TourTarget } from './tourSteps';

// The guided tour's state and its runner. Steps are data (tourSteps.ts); this module opens them,
// drives the Playground store, and runs the simulation fast while a step waits on it.

// prompt: the first-visit offer · confirm: asking before a reset · step: a card is showing ·
// waiting: hidden while the simulation runs to a condition.
export type TourPhase = 'off' | 'prompt' | 'confirm' | 'step' | 'waiting';

export const WAIT_SPEED = 10;

interface TourState {
  phase: TourPhase;
  index: number;
  ring: string | null; // canvas node id with the pulsing ring
  outline: TourControl | null; // sidebar control with the outline
  frame: { ids: string[] | null; seq: number }; // a request to fit the canvas (null ids = whole graph)
  waitLabel: string | null;
  // How the last wait ended (the end-to-end test checks that conditions fire before their fallback).
  lastAdvance: 'until' | 'fallback' | 'hurry' | null;

  offer(): void;
  requestStart(): void;
  start(): void;
  primary(): void;
  back(): void;
  finish(): void;
  skip(): void;
  decline(): void;
  hurry(): void;
}

// ─── Runner internals (module scope: timers and subscriptions aren't state) ──

let timers: ReturnType<typeof setTimeout>[] = [];
let unsubscribe: (() => void) | null = null;
let hurryNow: (() => void) | null = null;
let visitorSpeed: number | null = null;

function clearPending(): void {
  for (const t of timers) clearTimeout(t);
  timers = [];
  unsubscribe?.();
  unsubscribe = null;
  hurryNow = null;
}

function later(ms: number, fn: () => void): void {
  timers.push(setTimeout(fn, ms));
}

const play = () => usePlaygroundStore.getState();

export const useTourStore = create<TourState>((set, get) => {
  function focus(target: TourTarget, frame?: 'all' | 'target'): void {
    const s = play();
    const rippleId = target.ripple ? rippleByName(s, target.ripple) : null;
    const pondId = target.pond ? pondByName(s, target.pond) : null;
    if (rippleId) s.selectRipple(rippleId);
    else if (pondId) s.selectPond(pondId);
    else s.clearSelection();
    const node = rippleId ?? pondId;
    const whole = frame === 'all' || !node;
    set((t) => ({
      ring: node,
      outline: target.control ?? null,
      frame: { ids: whole ? null : [node], seq: t.frame.seq + 1 },
    }));
  }

  function open(index: number): void {
    clearPending();
    const step = TOUR_STEPS[index];
    set({ phase: 'step', index, waitLabel: null, ring: null, outline: null });
    const s = play();
    s.setTideDraft(null);
    focus(step.target ?? {}, step.frame);
    step.setup?.({ store: play(), focus });
    watchAction(step);
  }

  // A visitor who presses the real control instead of the card's button moves the tour on too.
  function watchAction(step: TourStep): void {
    const action = step.action;
    if (!action || action.done(play())) return;
    unsubscribe = usePlaygroundStore.subscribe((s) => {
      if (get().phase === 'step' && action.done(s)) {
        unsubscribe?.();
        unsubscribe = null;
        get().primary();
      }
    });
  }

  function wait(index: number, step: TourStep): void {
    const w = step.advance;
    if (w === 'next') return;
    clearPending();
    const since = play();
    if (since.paused) since.togglePause();
    since.setSpeed(w.speed ?? WAIT_SPEED);
    set((t) => ({ phase: 'waiting', waitLabel: w.label, frame: { ids: null, seq: t.frame.seq + 1 } }));
    let resolved = false;
    const done = (how: 'until' | 'fallback' | 'hurry') => {
      if (resolved) return;
      resolved = true;
      clearPending();
      play().setSpeed(1);
      set({ lastAdvance: how });
      open(index + 1);
    };
    hurryNow = () => done('hurry');
    unsubscribe = usePlaygroundStore.subscribe((s) => {
      if (w.until(s, since)) done('until');
    });
    later(w.fallbackMs, () => done('fallback'));
  }

  function end(reason: 'done' | 'skipped'): void {
    const { index, phase } = get();
    clearPending();
    if (visitorSpeed !== null) play().setSpeed(visitorSpeed);
    visitorSpeed = null;
    play().setTideDraft(null);
    set({ phase: 'off', ring: null, outline: null, waitLabel: null });
    writeTourMemory(reason);
    const step = phase === 'prompt' || phase === 'confirm' ? 0 : index + 1;
    track(reason === 'done' ? 'tour_complete' : 'tour_skip', { step });
  }

  return {
    phase: 'off',
    index: 0,
    ring: null,
    outline: null,
    frame: { ids: null, seq: 0 },
    waitLabel: null,
    lastAdvance: null,

    offer() {
      set({ phase: 'prompt' });
    },

    requestStart() {
      if (play().isDemoGraph()) get().start();
      else set({ phase: 'confirm' });
    },

    start() {
      clearPending();
      const s = play();
      if (visitorSpeed === null) visitorSpeed = s.speed;
      s.resetToDemo();
      if (s.paused) s.togglePause();
      s.setSpeed(1);
      track('tour_start');
      open(0);
    },

    // The card's main button: run the step's action if it hasn't been done, then move on (through
    // the step's wait, if it has one).
    primary() {
      const { index, phase } = get();
      if (phase !== 'step') return;
      clearPending(); // drop the action watcher before the action itself would trigger it
      const step = TOUR_STEPS[index];
      if (step.action && !step.action.done(play())) step.action.run(play());
      if (index === TOUR_STEPS.length - 1) return end('done');
      if (step.advance !== 'next') return wait(index, step);
      open(index + 1);
    },

    back() {
      const { index, phase } = get();
      if (phase === 'step' && index > 0) open(index - 1);
    },

    finish() {
      end('done');
    },

    skip() {
      end('skipped');
    },

    decline() {
      // From the prompt (remembered) or the reset confirmation (not remembered: they asked for it).
      if (get().phase === 'prompt') end('skipped');
      else set({ phase: 'off' });
    },

    hurry() {
      hurryNow?.();
    },
  };
});

// The highlight's flash class alternates by step, so its animation restarts on every step even when
// the same element stays highlighted (a changed animation-name restarts it).
function flashClass(index: number): string {
  return `ds-tour-flash-${index % 2}`;
}

// The class for a canvas node: the ring while the current step targets it.
export function useTourRing(nodeId: string): string | undefined {
  return useTourStore((t) => (t.ring === nodeId && t.phase !== 'off' ? `ds-tour-ring ${flashClass(t.index)}` : undefined));
}

// Props for a sidebar control the tour can point at: its `data-tour` name, plus the outline class
// while the current step targets it.
export function useTourMark(control: TourControl): { 'data-tour': TourControl; className?: string } {
  const className = useTourStore((t) =>
    t.outline === control && t.phase !== 'off' ? `ds-tour-outline ${flashClass(t.index)}` : undefined
  );
  return { 'data-tour': control, className };
}
