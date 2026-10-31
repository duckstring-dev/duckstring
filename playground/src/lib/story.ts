import type { PlaygroundState } from './store';
import type { PondId } from './types';
import { pondByName, rippleByName } from './tourSteps';

// The story: a two-minute, hands-off run through what Duckstring is, embedded on the landing page (/story). It is the
// launch video as a page, so it fits any screen and the viewer sets the pace: each line shows large over the paused,
// dimmed pipeline, then shrinks into a caption at the foot while the thing it introduces plays out.
//
// The script drives the Playground's own store and canvas through StoryCtx (implemented by the Story component). The
// viewer can step forward or back a line at a time: a jump replays the lines before it off screen in simulated time
// (jumpCtx), and plays on from there.

export interface SayOptions {
  during?: () => Promise<void>; // runs while the line is up (behind the dim), for changes not meant to be watched
  run?: boolean; // keep the pipeline running behind the line (default: paused while it's read)
  stay?: boolean; // leave the line up, centred, rather than docking it (the next line replaces it)
}

export interface StoryCtx {
  opening: boolean; // say the opening question (false when the viewer has just read it, over the Play button)
  get(): PlaygroundState;
  say(text: string, opts?: SayOptions): Promise<void>;
  wait(ms: number): Promise<void>; // story time: stops while the story is paused
  until(cond: (s: PlaygroundState) => boolean, minMs?: number): Promise<void>;
  ring(ids: string[]): void; // ring these canvas nodes (Pond or Ripple ids); [] clears
  chart(pond: PondId | null): Promise<void>; // show a Pond's run chart at the foot (the graph moves up), or hide it
  grid(step: number | null, eq?: string): void; // the Trickle join grid at a step, or hidden
  speed(x: number): void; // simulation speed (2 shows a badge)
}

export const STORY_LINES = {
  question: 'What if pipelines ran on demand from the end, instead of on a schedule from the start?',
  shared: 'These two outputs share an upstream step.',
  rates: 'They can ask for data at different rates,',
  supply: 'and the shared step runs only as often as needed to supply both.',
  continuous: 'Running one continuously,',
  paced: 'every step runs only as fast as the bottleneck, both upstream and downstream.',
  breaking: 'A breaking change runs as a new version alongside the old one.',
  harder: 'so the harder paths can move on their own schedule.',
  trickle: "And with incremental processing built on DuckDB, using DBSP's Z\u2011sets,", // a non-breaking hyphen
  work: 'you only do the work that you need.',
};

// How long a line holds, centred, before it docks (story ms).
export function holdMs(text: string): number {
  return Math.max(1900, 900 + text.split(/\s+/).length * 230);
}

const NEW_POND = 'forecast';

// The graph the story starts from: the demo pipeline plus a second output reading `sales`. `get` reads the store
// afresh after each change (a reset mints new ids).
export function setUpStory(get: () => PlaygroundState): void {
  get().resetToDemo();
  const sales = pondByName(get(), 'sales');
  if (sales) get().addPond({ name: NEW_POND, rippleName: 'trend', source: sales });
  get().clearSelection();
}

function runs(s: PlaygroundState, ref: string): number {
  const id = pondByName(s, ref);
  return id ? s.pondStates[id]?.runsCompleted ?? 0 : 0;
}

// Switching `reports` to continuous 3.6 s to 5 s (sim) after it last completed lands in the fresher of the
// throttle's two steady rhythms, whatever `forecast`'s 18 s cycle is doing; other moments can lock a rhythm about
// 2 s staler (see bench/launch_alignment and the story test). So the story waits for that moment.
export function inSwitchWindow(s: PlaygroundState): boolean {
  const id = pondByName(s, 'reports');
  const done = id ? s.pondStates[id]?.completionTimes ?? [] : [];
  if (!done.length) return false;
  const since = s.now - done[done.length - 1];
  return since >= 3600 && since < 5000;
}

// A new major of `pond` reading `sales` v2, its Refresh moved across, the old major removed (and, last, `sales` v1).
async function upgrade(c: StoryCtx, pond: string, removeOldSales: boolean): Promise<void> {
  const s = c.get();
  const from = pondByName(s, pond);
  const salesV1 = pondByName(s, 'sales');
  const salesV2 = pondByName(s, 'sales@2');
  if (!from || !salesV2) return;
  const to = s.addMajorVersion(from, s.ponds[from].sources.map((src) => (src === salesV1 ? salesV2 : src)));
  c.get().clearSelection();
  c.ring([to]);
  await c.wait(1300);
  c.get().moveRefresh(from, to);
  await c.wait(1200);
  c.get().deletePond(from);
  await c.wait(800);
  if (removeOldSales) {
    await c.wait(700);
    const old = pondByName(c.get(), 'sales');
    if (old) c.get().deletePond(old);
    await c.wait(800);
  }
  c.ring([]);
}

export type Scene = (c: StoryCtx) => Promise<void>;

const pond = (c: StoryCtx, ref: string) => pondByName(c.get(), ref) as PondId;

export const STORY_SCENES: Scene[] = [
  // 1. The question, which gives way straight to the next line; then two outputs, two rates, one shared step.
  async (c) => {
    const id = (ref: string) => pond(c, ref);
    if (c.opening) await c.say(STORY_LINES.question, { stay: true });
    await c.say(STORY_LINES.shared);
    c.ring([id('reports'), id(NEW_POND)]);
    await c.wait(1700);
    c.ring([id('sales')]);
    await c.wait(1700);
    c.ring([]);
    await c.say(STORY_LINES.rates);
    c.get().setRefresh(id('reports'), { everyMs: 6000 });
    c.get().setRefresh(id(NEW_POND), { everyMs: 18000 });
    c.ring([id('reports'), id(NEW_POND)]);
    await c.wait(2200);
    c.ring([]);
    await c.wait(4800); // the first requests travel up and the runs come back down, at real speed
    c.speed(2);
    await c.wait(3200);
    await c.say(STORY_LINES.supply, { during: () => c.chart(id('sales')) });
    c.ring([id('sales')]);
    await c.until((s) => runs(s, NEW_POND) >= 2, 7000);
    await c.wait(700);
    c.ring([]);
  },

  // 2. Continuous.
  async (c) => {
    const id = (ref: string) => pond(c, ref);
    await c.say(STORY_LINES.continuous, {
      run: true,
      during: async () => {
        await c.chart(null);
        await c.until(inSwitchWindow);
        c.get().setRefresh(id('reports'), { everyMs: 0 });
      },
    });
    c.ring([id('reports')]);
    await c.wait(1800);
    c.ring([]);
    await c.wait(5000);
    await c.say(STORY_LINES.paced, { during: () => c.chart(id('reports')) });
    const join = rippleByName(c.get(), 'sales.join_lines');
    if (join) c.ring([join]);
    await c.wait(7500);
    c.ring([]);
  },

  // 3. Upgrades.
  async (c) => {
    const id = (ref: string) => pond(c, ref);
    await c.say(STORY_LINES.breaking, {
      during: async () => {
        await c.chart(null);
        c.speed(1);
      },
    });
    const salesV2 = c.get().addMajorVersion(id('sales'));
    c.get().clearSelection();
    c.ring([salesV2]);
    await c.wait(2400);
    c.ring([]);
    await upgrade(c, 'reports', false);
    await c.wait(2500);
    await c.say(STORY_LINES.harder);
    await upgrade(c, NEW_POND, true);
    await c.wait(3000);
  },

  // 4. Trickle: the join grid, as in the Trickle tour.
  async (c) => {
    await c.say(STORY_LINES.trickle);
    c.grid(0);
    await c.wait(2400);
    c.grid(1, 'A′ ⋈ B′ = A ⋈ B + ΔA ⋈ B + A ⋈ ΔB + ΔA ⋈ ΔB');
    await c.wait(2800);
    c.grid(2);
    await c.wait(2600);
    c.grid(3);
    await c.wait(3200);
    c.grid(null);
    await c.wait(500);
    await c.say(STORY_LINES.work, { stay: true });
  },
];

// The story's last line, which stays up over the end buttons.
export const LAST_LINE = STORY_LINES.work;

// What the screen carries into a line from the ones before it.
export interface Reached {
  speed: number;
  chart: PondId | null;
}

// A StoryCtx with nothing on screen, in simulated time. It keeps the timing the Story component's gives the pipeline
// (paused while a line is read unless it runs, `wait` at the story's speed, `until` in 100 ms steps), driving the
// store directly, so the state it reaches is the state the story would have shown.
export function headless(get: () => PlaygroundState, opening: boolean): { ctx: StoryCtx; reached: () => Reached } {
  let speed = 1;
  let chart: PondId | null = null;
  let holding = true;
  let shown = false;
  const advance = (ms: number) => {
    if (holding) return;
    const end = get().now + ms * speed;
    while (get().now < end) get().tick(Math.min(end, get().now + 100));
  };
  const ctx: StoryCtx = {
    opening,
    get,
    say: async (text, opts = {}) => {
      if (shown) advance(300);
      shown = true;
      holding = !opts.run;
      if (opts.during) await opts.during();
      advance(holdMs(text));
      if (opts.stay) return;
      holding = false;
      advance(450);
    },
    wait: async (ms) => advance(ms),
    until: async (cond, minMs = 0) => {
      // Bounded: a condition that never holds (or holds only while the pipeline is paused) gives up after 2 min.
      for (let t = 0; (t < minMs || !cond(get())) && t < 120_000; t += 100) advance(100);
    },
    ring: () => {},
    chart: async (p) => {
      chart = p;
      advance(700);
    },
    grid: () => {},
    speed: (x) => {
      speed = x;
    },
  };
  return { ctx, reached: () => ({ speed, chart }) };
}

export async function playStory(c: StoryCtx): Promise<void> {
  for (const scene of STORY_SCENES) await scene(c);
}

// The story from its `target`th line (counting the lines said, from 0): the lines before it play headless, at once,
// so it starts from the state the story would have reached, and from it on `live` plays. `onLive` gets that state as
// play switches over; `onLine` gets each live line's number and text as it starts.
export function jumpCtx(
  live: StoryCtx,
  target: number,
  hooks: { onLive: (reached: Reached) => void; onLine: (index: number, text: string) => void }
): StoryCtx {
  const h = headless(live.get, live.opening);
  let n = 0;
  let on = target <= 0;
  const pick = () => (on ? live : h.ctx);
  return {
    opening: live.opening,
    get: live.get,
    say: async (text, opts) => {
      if (!on && n === target) {
        on = true;
        hooks.onLive(h.reached());
      }
      const i = n++;
      if (!on) return h.ctx.say(text, opts);
      hooks.onLine(i, text);
      return live.say(text, opts);
    },
    wait: (ms) => pick().wait(ms),
    until: (cond, minMs) => pick().until(cond, minMs),
    ring: (ids) => pick().ring(ids),
    chart: (p) => pick().chart(p),
    grid: (step, eq) => pick().grid(step, eq),
    speed: (x) => pick().speed(x),
  };
}
