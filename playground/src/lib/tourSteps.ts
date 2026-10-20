import type { Choice } from '@/components/NextChoices';
import type { Mode } from './mode';
import type { PondId, RippleId } from './types';
import type { PlaygroundState } from './store';

// The guided tour's steps, as data. Copy lives here so it can be reviewed without touching
// components. Names in `backticks` render as code. Targets name Ponds and Ripples by their names in
// the example graph and are resolved to ids when a step opens.

// Sidebar controls the tour can outline. Each matches a `data-tour` attribute in the Sidebar.
export type TourControl = 'refresh-now' | 'refresh-every' | 'refresh-continuous' | 'new-major' | 'ripple-duration' | 'ripple-variability' | 'run-chart';

export interface TourTarget {
  pond?: string; // a Pond name, or "name@major" (a bare name is major 1)
  ripple?: string; // "pond.ripple", the pond part as above
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
  speedAfter?: { ms: number; speed: number; label?: string }; // change the speed (and label) this long (real time) into the wait
  keepFocus?: boolean; // keep the step's target selected and in view while waiting
  focus?: TourTarget; // select and ring this instead while waiting (implies keepFocus)
  show?: TourControl; // with keepFocus: outline this sidebar control and scroll it into view while waiting
  expectedMs: number; // typical real time it takes, measured in the browser: paces the progress bar
}

// One stage of an animated action: it runs `ms` (real time) after the previous one.
export interface TourStage {
  ms: number;
  run: (ctx: TourContext) => void;
}

export interface TourStep {
  id: string;
  chapter: 1 | 2 | 3 | 4;
  intro?: boolean; // a chapter's opening card: its title is the chapter's
  title: string;
  body: string; // one or two sentences ('' on a chapter's opening card)
  target?: TourTarget;
  // Default: the target's node if it has one, else the whole graph. 'keep' leaves the view and selection as the
  // previous step left them, with its highlights cleared (a chapter's opening card, so its last chart stays in view).
  frame?: 'all' | 'target' | 'keep';
  setup?: (ctx: TourContext) => void; // runs when the step opens, after `target` is applied
  // 'next' moves straight on; a wait hides the tour and runs the simulation fast until `until` holds.
  advance: 'next' | TourWait;
  // "Do it for me": the primary button runs this (or plays its stages). If the visitor does it themselves, the
  // tour moves on.
  action?: { label: string; run?: (s: PlaygroundState) => void; stages?: TourStage[]; done: (s: PlaygroundState) => boolean };
  suggestions?: string[]; // a bulleted list after the body
  footer?: string; // a closing line after the suggestions
  // A link opens `href` in a new tab, or with `mode` finishes the tour and switches the Playground's mode.
  links?: { label: string; href?: string; mode?: Mode }[];
  // On the last step: the ways on, in place of the buttons (see NextChoices).
  choices?: Choice[];
}

export const CHAPTER_TITLES: Record<TourStep['chapter'], string> = {
  1: 'Running Pipelines',
  2: 'Repeated Refresh',
  3: 'Multiple Triggers',
  4: 'Upgrades',
};

// ─── Name resolution ─────────────────────────────────────────────────────────

// "name" is major 1 (or the only Pond of that name); "name@major" names a major line.
export function pondByName(s: PlaygroundState, ref: string): PondId | null {
  const [name, major] = ref.split('@');
  const named = Object.values(s.ponds).filter((p) => p.name === name);
  if (major != null) return named.find((p) => (p.major ?? 1) === Number(major))?.id ?? null;
  return (named.find((p) => (p.major ?? 1) === 1) ?? (named.length === 1 ? named[0] : undefined))?.id ?? null;
}

export function rippleByName(s: PlaygroundState, ref: string): RippleId | null {
  const dot = ref.lastIndexOf('.');
  const pid = pondByName(s, ref.slice(0, dot));
  const ripple = ref.slice(dot + 1);
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

function pondRunsSince(s: PlaygroundState, since: PlaygroundState, ref: string): number {
  const id = pondByName(s, ref);
  if (!id) return Infinity;
  return (s.pondStates[id]?.runsCompleted ?? 0) - (since.pondStates[id]?.runsCompleted ?? 0);
}

function pondSettled(s: PlaygroundState, since: PlaygroundState, ref: string): boolean {
  const id = pondByName(s, ref);
  if (!id) return true;
  return settled(s.pondStates[id]?.completionTimes, pondRunsSince(s, since, ref));
}

function rippleRunsSince(s: PlaygroundState, since: PlaygroundState, ref: string): number {
  const id = rippleByName(s, ref);
  if (!id) return Infinity;
  return (s.rippleStates[id]?.runsCompleted ?? 0) - (since.rippleStates[id]?.runsCompleted ?? 0);
}

// ─── Actions ─────────────────────────────────────────────────────────────────

const NOISE = 0.2;
const NEW_POND = 'forecast';
const STAGE_MS = 1200;

function hasRefresh(s: PlaygroundState, ref: string, everyMs?: number): boolean {
  const id = pondByName(s, ref);
  const refs = id ? s.refreshes[id] ?? [] : [];
  return everyMs == null ? refs.length > 0 : refs.some((r) => r.startAnchor == null && r.everyMs === everyMs);
}

function setRefreshEvery(s: PlaygroundState, ref: string, everyMs: number): void {
  const id = pondByName(s, ref);
  if (id) s.setRefresh(id, { everyMs });
}

function setJoinVariability(s: PlaygroundState, v: number): void {
  const id = rippleByName(s, 'sales.join_lines');
  if (!id || s.ripples[id].variability === v) return;
  s.setRippleVariability(id, v);
  s.bumpFormEpoch();
}

// Ring and frame a Pond during an animation without opening the sidebar on it.
function point(ctx: TourContext, ref: string, frame: 'all' | 'target' = 'all'): void {
  ctx.focus({ pond: ref }, frame);
  ctx.store.clearSelection();
  ctx.store.setSheetOpen(false);
}

function readsFrom(s: PlaygroundState, sink: string, source: string): boolean {
  const k = pondByName(s, sink);
  const src = pondByName(s, source);
  return !!k && !!src && s.ponds[k].sources.includes(src);
}

// An upgrade animated stage by stage: a new major of `pond` reading `sales` v2 in place of v1, its Refresh moved
// across, the old major removed (and, last, `sales` v1 when nothing reads it any more).
function upgradeStages(pond: string, removeOldSales: boolean): TourStage[] {
  const stages: TourStage[] = [
    {
      ms: 400,
      run: (ctx) => {
        const s = ctx.store;
        const id = pondByName(s, pond);
        const salesV1 = pondByName(s, 'sales');
        const salesV2 = pondByName(s, 'sales@2');
        if (id && salesV2 && !pondByName(s, `${pond}@2`)) {
          s.addMajorVersion(id, s.ponds[id].sources.map((src) => (src === salesV1 ? salesV2 : src)));
        }
        point(ctx, `${pond}@2`);
      },
    },
    {
      ms: STAGE_MS,
      run: (ctx) => {
        const from = pondByName(ctx.store, pond);
        const to = pondByName(ctx.store, `${pond}@2`);
        if (from && to) ctx.store.moveRefresh(from, to);
        point(ctx, `${pond}@2`);
      },
    },
    {
      ms: STAGE_MS,
      run: (ctx) => {
        const old = pondByName(ctx.store, pond);
        if (old) ctx.store.deletePond(old);
        point(ctx, `${pond}@2`);
      },
    },
  ];
  if (removeOldSales) {
    stages.push({
      ms: STAGE_MS,
      run: (ctx) => {
        const old = pondByName(ctx.store, 'sales');
        if (old) ctx.store.deletePond(old);
        point(ctx, 'sales@2');
      },
    });
  }
  return stages;
}

function upgraded(s: PlaygroundState, pond: string): boolean {
  return !pondByName(s, pond) && readsFrom(s, `${pond}@2`, 'sales@2') && hasRefresh(s, `${pond}@2`);
}

// ─── Steps ───────────────────────────────────────────────────────────────────

export const TOUR_STEPS: TourStep[] = [
  // Chapter 1: running pipelines
  {
    id: 'intro-1',
    chapter: 1,
    intro: true,
    title: CHAPTER_TITLES[1],
    body: '',
    frame: 'all',
    advance: 'next',
  },
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
    id: 'refresh',
    chapter: 1,
    title: 'Refresh',
    body: 'A Refresh triggers this Pond to signal upstream to run from start to finish, bringing the data up to the **freshness** of now. This works like a Kanban process, sending demand to each parent until reaching a root (Inlet).',
    target: { pond: 'reports', control: 'refresh-now' },
    action: {
      label: 'Refresh now',
      run: (s) => {
        const id = pondByName(s, 'reports');
        if (id) s.refreshNow(id);
      },
      done: (s) => {
        const id = pondByName(s, 'reports');
        return !!id && (s.pondStates[id]?.targets.length > 0 || (s.pondStates[id]?.runsCompleted ?? 0) > 0);
      },
    },
    // The visitor's first look at the pipeline running, so at real speed: once through takes about 7 s.
    advance: {
      label: 'Watching the pipeline run once',
      speed: 1,
      expectedMs: 7300,
      until: (s, since) => pondRunsSince(s, since, 'reports') >= 1,
      fallbackMs: 20000,
    },
  },
  // Chapter 2: repeated refresh
  {
    id: 'intro-2',
    chapter: 2,
    intro: true,
    title: CHAPTER_TITLES[2],
    body: '',
    frame: 'keep',
    advance: 'next',
  },
  {
    id: 'refresh-every',
    chapter: 2,
    title: 'Refresh Every',
    body: 'That took about 7 s. A Refresh can be set to send every 6 s, for example, faster than back to back. More specifically, a Refresh is sent any time the data is less fresh than 6 s, or 6 s after the previous Refresh was requested.',
    target: { pond: 'reports', control: 'refresh-every' },
    action: {
      label: 'Refresh every 6 s',
      run: (s) => setRefreshEvery(s, 'reports', 6000),
      done: (s) => hasRefresh(s, 'reports', 6000),
    },
    // Real speed for 8 s (two Refreshes, one run end to end), then faster for enough runs that the run chart's
    // averages (the last six) rest on the steady state.
    advance: {
      label: 'Running a few rounds',
      speed: 1,
      speedAfter: { ms: 8000, speed: 10, label: 'Speeding up to steady state' },
      expectedMs: 11500,
      until: (s, since) => pondRunsSince(s, since, 'reports') >= 8 && pondSettled(s, since, 'reports'),
      fallbackMs: 25000,
    },
  },
  {
    id: 'steady',
    chapter: 2,
    title: 'Steady state',
    body: 'At steady state, every Ripple, upstream and downstream, runs every 6 s.',
    target: { pond: 'sales' },
    frame: 'all',
    advance: 'next',
  },
  {
    id: 'run-chart',
    chapter: 2,
    title: 'The run chart',
    body: "Each Ripple's run chart shows its run time and the time between its runs. Every one shows 6 s between runs. Try looking at the others too.",
    target: { pond: 'reports', control: 'run-chart' },
    frame: 'all',
    advance: 'next',
  },
  {
    id: 'continuous',
    chapter: 2,
    title: 'Continuous Refresh',
    body: 'What if the Refresh rate is faster than the pipeline can deliver? Or in the extreme, every 0 s?',
    target: { pond: 'reports', control: 'refresh-continuous' },
    action: {
      label: 'Refresh continuously',
      run: (s) => setRefreshEvery(s, 'reports', 0),
      done: (s) => hasRefresh(s, 'reports', 0),
    },
    // Real speed for 6 s (two cycles), then faster for a dozen more runs, so the chart shows a steady state and its
    // averages rest on it. `reports`' run chart stays in view.
    advance: {
      label: 'Watching the pipeline settle',
      speed: 1,
      speedAfter: { ms: 6000, speed: 10, label: 'Speeding up until it settles' },
      keepFocus: true,
      show: 'run-chart',
      expectedMs: 9800,
      until: (s, since) =>
        pondRunsSince(s, since, 'reports') >= 14 &&
        rippleSettled(s, since, 'reports.monthly_summary') &&
        rippleSettled(s, since, 'transactions.ingest'),
      fallbackMs: 30000,
    },
  },
  {
    id: 'throttled',
    chapter: 2,
    title: 'Throttled',
    body: "Throttling the Refreshes that can't be delivered in time, at steady state, every Ripple, upstream and downstream, runs every 3 s. `sales` takes 5.5 s from start to finish, so two of its runs are in flight at once.",
    target: { pond: 'reports', control: 'run-chart' },
    advance: 'next',
  },
  {
    id: 'taps',
    chapter: 2,
    title: 'Refresh on Request',
    body:
      "Given the pipeline can handle any Refresh rate, this opens up the potential to link query demand to execution. By allowing a 'Refresh on Request' (a **Tap**), update frequency can be tied to query frequency.",
    frame: 'keep',
    advance: 'next',
  },

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
      focus: { pond: 'sales' },
      show: 'run-chart',
      expectedMs: 8700,
      until: (s, since) => rippleRunsSince(s, since, 'transactions.ingest') >= 24,
      fallbackMs: 30000,
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

  // Chapter 3: multiple triggers
  {
    id: 'intro-3',
    chapter: 3,
    intro: true,
    title: CHAPTER_TITLES[3],
    body: '',
    frame: 'keep',
    setup: ({ store }) => setJoinVariability(store, 0), // the cadences in this chapter are without noise
    advance: 'next',
  },
  {
    id: 'new-output',
    chapter: 3,
    title: 'A second output',
    body: `We've added a new output Pond, \`${NEW_POND}\`, reading from \`sales\`. It doesn't run, because nothing has a trigger on it. That's a key advantage of scheduling from the end: nothing runs unless something needs it.`,
    setup: (ctx) => {
      const s = ctx.store;
      setJoinVariability(s, 0); // the cadences in this chapter are without noise
      if (!pondByName(s, NEW_POND)) {
        const sales = pondByName(s, 'sales');
        s.addPond({ name: NEW_POND, rippleName: 'trend', source: sales ?? undefined });
      }
      ctx.focus({ pond: NEW_POND }, 'all');
    },
    advance: 'next',
  },
  {
    id: 'multiple-refreshes',
    chapter: 3,
    title: 'Multiple Refreshes',
    body: 'Adding another Refresh every 5 s, the continuous Refresh should approach 3 s and this one 5 s. They share the 3 s bottleneck, so the 5 s Refresh is met on average by cycling between 3 s and 6 s between runs.',
    target: { pond: NEW_POND, control: 'run-chart' },
    action: {
      label: 'Refresh every 5 s',
      run: (s) => setRefreshEvery(s, NEW_POND, 5000),
      done: (s) => hasRefresh(s, NEW_POND, 5000),
    },
    // Keeps `forecast` selected, so its run chart fills while the pipeline runs.
    advance: {
      label: 'Running fast for a few cycles',
      keepFocus: true,
      show: 'run-chart',
      expectedMs: 8000,
      until: (s, since) => pondRunsSince(s, since, NEW_POND) >= 14,
      fallbackMs: 30000,
    },
  },

  // Chapter 4: upgrades
  {
    id: 'intro-4',
    chapter: 4,
    intro: true,
    title: CHAPTER_TITLES[4],
    body: '',
    frame: 'keep',
    advance: 'next',
  },
  {
    id: 'upgrades',
    chapter: 4,
    title: 'A breaking change',
    body: 'If a breaking change (e.g. column delete) is introduced to `sales`, both `reports` and `forecast` would need to also update, which can be difficult to co-ordinate. Instead, we keep the old versions running and add a "v2" for `sales`',
    target: { pond: 'sales', control: 'new-major' },
    action: {
      label: 'Add sales v2',
      stages: [
        {
          ms: 400,
          run: (ctx) => {
            const sales = pondByName(ctx.store, 'sales');
            if (sales && !pondByName(ctx.store, 'sales@2')) ctx.store.addMajorVersion(sales);
            point(ctx, 'sales@2');
          },
        },
      ],
      done: (s) => !!pondByName(s, 'sales@2'),
    },
    advance: 'next',
  },
  {
    id: 'v2-reports',
    chapter: 4,
    title: 'V2 reports',
    body: "It's not running yet as there are no consumers. Now we can upgrade reports, move its Refresh, and remove the previous version. The `forecast` path can keep running until it can be upgraded on its own timeframe.",
    target: { pond: 'sales@2' },
    frame: 'all',
    action: { label: 'Upgrade reports', stages: upgradeStages('reports', false), done: (s) => upgraded(s, 'reports') },
    advance: {
      label: 'Running the upgraded path',
      expectedMs: 1600,
      until: (s, since) => pondRunsSince(s, since, 'reports@2') >= 2,
      fallbackMs: 15000,
    },
  },
  {
    id: 'v2-forecast',
    chapter: 4,
    title: 'V2 forecast',
    body: "Now say `forecast` v2 is ready. We can add it, move its Refresh, remove its version **and** the old `sales` version, now that there are no consumers. Note removal isn't strictly necessary, as nothing is consuming and it won't run - it's just to keep things tidy.",
    target: { pond: NEW_POND },
    frame: 'all',
    action: {
      label: 'Upgrade forecast',
      stages: upgradeStages(NEW_POND, true),
      done: (s) => upgraded(s, NEW_POND) && !pondByName(s, 'sales'),
    },
    advance: {
      label: 'Running the upgraded path',
      expectedMs: 1600,
      until: (s, since) => pondRunsSince(s, since, `${NEW_POND}@2`) >= 2,
      fallbackMs: 15000,
    },
  },
  {
    id: 'versioning',
    chapter: 4,
    title: 'Versioning',
    body: 'Duckstring uses strict SemVer:',
    suggestions: [
      'Major covers breaking changes, where upgrading would break downstream',
      'Minor covers extension of scope, where upgrading is fine, but downgrading could break downstream',
      'Patch covers any change that would not break upon either upgrade or downgrade',
    ],
    footer: 'Both data and execution are against each **major** Pond version.',
    frame: 'all',
    setup: ({ store }) => store.clearSelection(),
    advance: 'next',
  },
  {
    id: 'done',
    chapter: 4,
    title: "That's most of it",
    body: 'See the docs for more detail, or carry on to Trickle (incremental processing):',
    frame: 'all',
    setup: ({ store }) => store.clearSelection(),
    advance: 'next',
    choices: [
      { id: 'trickle', label: 'Trickle tour', detail: 'Incremental processing: each run touches only what changed', mode: 'trickle', tone: 'primary' },
      { id: 'quickstart', label: 'Quickstart', href: 'https://docs.duckstring.com/quickstart', tone: 'muted' },
      { id: 'docs', label: 'Docs', href: 'https://docs.duckstring.com/', tone: 'muted' },
      { id: 'github', label: 'GitHub', href: 'https://github.com/duckstring-dev/duckstring', tone: 'muted' },
      { id: 'close', label: 'Close tour', close: true, tone: 'quiet' },
    ],
  },
];
