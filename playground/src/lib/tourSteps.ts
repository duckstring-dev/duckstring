import type { PondId, RippleId } from './types';
import type { PlaygroundState } from './store';

// The guided tour's steps, as data. Copy lives here so it can be reviewed without touching
// components. Names in `backticks` render as code. Targets name Ponds and Ripples by their names in
// the example graph and are resolved to ids when a step opens.

// Sidebar controls the tour can outline. Each matches a `data-tour` attribute in the Sidebar.
export type TourControl = 'wave' | 'tide' | 'ripple-duration' | 'ripple-variability' | 'run-chart';

export interface TourTarget {
  pond?: string; // a Pond name
  ripple?: string; // "pond.ripple"
  control?: TourControl;
}

export interface TourContext {
  store: PlaygroundState;
  // Select and ring a target, outline its control and frame it ('all' frames the whole graph).
  focus(target: TourTarget, frame?: 'all' | 'target'): void;
}

export interface TourWait {
  // Checked on every simulation tick against the state when the wait began.
  until: (s: PlaygroundState, since: PlaygroundState) => boolean;
  fallbackMs: number; // real time, so the tour can't hang
  label: string; // shown while the tour is hidden
  speed?: number; // simulation speed while waiting (default WAIT_SPEED)
}

export interface TourStep {
  id: string;
  chapter: 1 | 2 | 3;
  title: string;
  body: string; // one or two sentences
  target?: TourTarget;
  frame?: 'all' | 'target'; // default: the target's node if it has one, else the whole graph
  setup?: (ctx: TourContext) => void; // runs when the step opens, after `target` is applied
  // 'next' moves straight on; a wait hides the tour and runs the simulation fast until `until` holds.
  advance: 'next' | TourWait;
  // "Do it for me": the primary button runs this. If the visitor does it themselves, the tour moves on.
  action?: { label: string; run: (s: PlaygroundState) => void; done: (s: PlaygroundState) => boolean };
  links?: { label: string; href: string }[];
}

export const CHAPTER_TITLES: Record<TourStep['chapter'], string> = {
  1: "Running at the bottleneck's pace",
  2: 'Noise',
  3: 'Fixed frequencies and several outputs',
};

// ─── Name resolution ─────────────────────────────────────────────────────────

export function pondByName(s: PlaygroundState, name: string): PondId | null {
  return Object.values(s.ponds).find((p) => p.name === name)?.id ?? null;
}

export function rippleByName(s: PlaygroundState, ref: string): RippleId | null {
  const [pond, ripple] = ref.split('.');
  const pid = pondByName(s, pond);
  return Object.values(s.ripples).find((r) => r.pondId === pid && r.name === ripple)?.id ?? null;
}

// ─── Wait conditions ─────────────────────────────────────────────────────────

const SETTLED_TOLERANCE_MS = 250;

// The last `n` gaps between completions agree to within the tolerance, and at least `n + 1` runs
// completed since the wait began (so history from before it can't satisfy it).
function settled(times: number[] | undefined, newRuns: number, n = 3): boolean {
  if (!times || newRuns < n + 1 || times.length < n + 1) return false;
  const last = times.slice(-(n + 1));
  const gaps = last.slice(1).map((t, i) => t - last[i]);
  return Math.max(...gaps) - Math.min(...gaps) <= SETTLED_TOLERANCE_MS;
}

function rippleSettled(s: PlaygroundState, since: PlaygroundState, ref: string): boolean {
  const id = rippleByName(s, ref);
  if (!id) return true;
  return settled(s.rippleStates[id]?.completionTimes, rippleRunsSince(s, since, ref));
}

function pondSettled(s: PlaygroundState, since: PlaygroundState, name: string): boolean {
  const id = pondByName(s, name);
  if (!id) return true;
  const newRuns = (s.pondStates[id]?.runsCompleted ?? 0) - (since.pondStates[id]?.runsCompleted ?? 0);
  return settled(s.pondStates[id]?.completionTimes, newRuns);
}

function rippleRunsSince(s: PlaygroundState, since: PlaygroundState, ref: string): number {
  const id = rippleByName(s, ref);
  if (!id) return Infinity;
  return (s.rippleStates[id]?.runsCompleted ?? 0) - (since.rippleStates[id]?.runsCompleted ?? 0);
}

// ─── Actions ─────────────────────────────────────────────────────────────────

const TIDE_SECONDS = 5;
const NOISE = 0.2;
const NEW_POND = 'forecast';

function hasTrigger(s: PlaygroundState, pond: string, kind: 'wave' | 'tide'): boolean {
  const id = pondByName(s, pond);
  return !!id && s.triggers[id]?.kind === kind;
}

function setTide(s: PlaygroundState, pond: string): void {
  const id = pondByName(s, pond);
  if (!id) return;
  s.triggerTide(id, TIDE_SECONDS * 1000);
  s.setTideDraft(null);
}

function setJoinVariability(s: PlaygroundState, v: number): void {
  const id = rippleByName(s, 'sales.join_lines');
  if (!id || s.ripples[id].variability === v) return;
  s.setRippleVariability(id, v);
  s.bumpFormEpoch();
}

// ─── Steps ───────────────────────────────────────────────────────────────────

export const TOUR_STEPS: TourStep[] = [
  // Chapter 1: running at the bottleneck's pace
  {
    id: 'pipeline',
    chapter: 1,
    title: 'An example pipeline',
    body: "Here's an example pipeline. It's split into four modules called Ponds, and each Pond contains steps called Ripples.",
    frame: 'all',
    advance: 'next',
  },
  {
    id: 'bottleneck',
    chapter: 1,
    title: 'The slowest step',
    body: 'Each Ripple shows how long it takes to run. `join_lines` is the longest, at 3 s, so the most often this pipeline could run is every 3 s.',
    target: { ripple: 'sales.join_lines', control: 'ripple-duration' },
    advance: 'next',
  },
  {
    id: 'shorter-ripples',
    chapter: 1,
    title: 'Shorter steps',
    body: "Ideally, shorter Ripples like this one wouldn't run more often than that.",
    target: { ripple: 'transactions.ingest' },
    advance: 'next',
  },
  {
    id: 'from-the-end',
    chapter: 1,
    title: 'Running from the end',
    body: 'To do this, Duckstring runs from the end of the pipeline instead of the beginning, sending demand to everything upstream.',
    target: { pond: 'reports' },
    advance: 'next',
  },
  {
    id: 'wave',
    chapter: 1,
    title: 'A Wave',
    body: 'A Wave trigger sends demand every time its Pond finishes, so the pipeline runs as often as it can.',
    target: { pond: 'reports', control: 'wave' },
    action: {
      label: 'Start a Wave',
      run: (s) => {
        const id = pondByName(s, 'reports');
        if (id) s.triggerWave(id);
      },
      done: (s) => hasTrigger(s, 'reports', 'wave'),
    },
    // The visitor's first look at the pipeline running, so at real speed: it settles in about 20 s.
    advance: {
      label: 'Watching the pipeline start up',
      speed: 1,
      until: (s, since) =>
        rippleSettled(s, since, 'reports.monthly_summary') && rippleSettled(s, since, 'transactions.ingest'),
      fallbackMs: 40000,
    },
  },
  {
    id: 'steady',
    chapter: 1,
    title: 'Steady state',
    body: 'At steady state, every Ripple, upstream and downstream, runs every 3 s. `sales` takes 5 s from start to finish, so two of its runs are in flight at once.',
    target: { pond: 'sales' },
    frame: 'all',
    advance: 'next',
  },
  {
    id: 'run-chart',
    chapter: 1,
    title: 'The run chart',
    body: "Each Ripple's run chart shows its run time and the time between its runs. Every one shows 3 s between runs.",
    target: { ripple: 'transactions.ingest', control: 'run-chart' },
    advance: 'next',
  },

  // Chapter 2: noise
  {
    id: 'noise',
    chapter: 2,
    title: 'Add some noise',
    body: "Let's make the bottleneck's run time vary randomly.",
    target: { ripple: 'sales.join_lines', control: 'ripple-variability' },
    action: {
      label: `Set σ to ${NOISE}`,
      run: (s) => setJoinVariability(s, NOISE),
      done: (s) => {
        const id = rippleByName(s, 'sales.join_lines');
        return !!id && s.ripples[id].variability > 0;
      },
    },
    advance: {
      label: 'Running fast for a few cycles',
      until: (s, since) => rippleRunsSince(s, since, 'transactions.ingest') >= 8,
      fallbackMs: 10000,
    },
  },
  {
    id: 'adapts',
    chapter: 2,
    title: 'The pipeline adapts',
    body: 'The pipeline follows the varying bottleneck. Nothing is tuned and nothing plans globally: each step only reacts to the steps next to it.',
    target: { ripple: 'transactions.ingest', control: 'run-chart' },
    advance: 'next',
  },

  // Chapter 3: fixed frequencies and several outputs
  {
    id: 'tide',
    chapter: 3,
    title: 'A Tide',
    body: `Sometimes you don't need data as fresh as possible, just at a set frequency, such as daily. That's what a Tide is for. With the noise turned off again, let's set one for every ${TIDE_SECONDS} s.`,
    target: { pond: 'reports', control: 'tide' },
    setup: ({ store }) => {
      setJoinVariability(store, 0);
      store.setTideDraft(String(TIDE_SECONDS));
    },
    action: {
      label: `Set a ${TIDE_SECONDS} s Tide`,
      run: (s) => setTide(s, 'reports'),
      done: (s) => hasTrigger(s, 'reports', 'tide'),
    },
    advance: {
      label: 'Running fast until the Tide settles',
      until: (s, since) => pondSettled(s, since, 'reports'),
      fallbackMs: 15000,
    },
  },
  {
    id: 'tide-explained',
    chapter: 3,
    title: `Every ${TIDE_SECONDS} s`,
    body: `Every ${TIDE_SECONDS} s, the Tide asks for data newer than it last asked for, and the request flows from the beginning of the pipeline to \`reports\`. Its runs now land every ${TIDE_SECONDS} s.`,
    target: { pond: 'reports', control: 'run-chart' },
    frame: 'all',
    advance: 'next',
  },
  {
    id: 'new-output',
    chapter: 3,
    title: 'A second output',
    body: `We've added a new output Pond, \`${NEW_POND}\`, reading from \`sales\`. It doesn't run, because nothing has a trigger on it. That's a key advantage of scheduling from the end: nothing runs unless something needs it.`,
    setup: (ctx) => {
      const s = ctx.store;
      if (!pondByName(s, NEW_POND)) {
        const sales = pondByName(s, 'sales');
        s.addPond({ name: NEW_POND, rippleName: 'trend', source: sales ?? undefined });
      }
      ctx.focus({ pond: NEW_POND }, 'all');
    },
    advance: 'next',
  },
  {
    id: 'done',
    chapter: 3,
    title: "That's most of it",
    body: 'The docs cover the rest, from Windows and failures to versioning and incremental processing.',
    frame: 'all',
    setup: ({ store }) => store.clearSelection(),
    advance: 'next',
    links: [
      { label: 'Docs', href: 'https://docs.duckstring.com/' },
      { label: 'Quickstart', href: 'https://docs.duckstring.com/quickstart' },
    ],
  },
];
