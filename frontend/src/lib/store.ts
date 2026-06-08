import { create } from 'zustand';
import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  NodeView,
  TriggerView,
  PondInfo,
  PondRun,
  WindowRow,
} from './types';
import {
  fetchStatus,
  fetchRuns,
  fetchWindows,
  postTrigger,
  addWindow,
  removeWindow,
  type StatusPayload,
  type RawPond,
  type RawRipple,
  type RawPondRun,
  type RawWindow,
  type AddWindowBody,
} from './api';

// ─── Payload → view-model transforms ─────────────────────────────────────────

// Parse a backend timestamp to ms. Most are tz-aware ISO, but SQLite `datetime('now')` defaults are
// naive UTC ("YYYY-MM-DD HH:MM:SS"); a bare Date.parse would read those as *local* time. Normalise:
// space→T and append 'Z' when no offset is present, so everything is interpreted as UTC.
export function parseTs(ts: string | null): number {
  if (!ts) return 0;
  const s = ts.includes('T') ? ts : ts.replace(' ', 'T');
  const hasTz = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s);
  return Date.parse(hasTz ? s : `${s}Z`);
}

const isoToMs = (iso: string | null): number => parseTs(iso);
const isoToMsOrNull = (iso: string | null): number | null => (iso ? parseTs(iso) : null);

function nodeView(n: RawPond | RawRipple, dMs: number): NodeView {
  return {
    status: n.status,
    startF: isoToMs(n.start_f),
    endF: isoToMs(n.end_f),
    targetF: isoToMsOrNull(n.target_f),
    hasPull: n.has_pull,
    runsStarted: n.gen,
    runsCompleted: n.runs_completed,
    dMs,
  };
}

function mapRun(r: RawPondRun): PondRun {
  return {
    pond: r.pond,
    version: r.version,
    f: r.f,
    startedAt: r.started_at,
    finishedAt: r.finished_at,
    status: r.status,
    ripples: r.ripples?.map((rr) => ({
      ripple: rr.ripple,
      startedAt: rr.started_at,
      finishedAt: rr.finished_at,
      status: rr.status,
    })),
  };
}

function mapWindow(w: RawWindow): WindowRow {
  return {
    name: w.name,
    startAnchor: w.start_anchor,
    durationSeconds: w.duration_seconds,
    freqUnit: w.freq_unit,
    freqInterval: w.freq_interval,
    validDays: w.valid_days,
    untilTime: w.until_time,
  };
}

interface StatusSlice {
  ponds: Record<PondId, Pond>;
  ripples: Record<RippleId, Ripple>;
  pondViews: Record<PondId, NodeView>;
  rippleViews: Record<RippleId, NodeView>;
  pondInfo: Record<PondId, PondInfo>;
  triggers: Record<PondId, TriggerView>;
}

function transformStatus(payload: StatusPayload): StatusSlice {
  const ponds: Record<PondId, Pond> = {};
  const ripples: Record<RippleId, Ripple> = {};
  const pondViews: Record<PondId, NodeView> = {};
  const rippleViews: Record<RippleId, NodeView> = {};
  const pondInfo: Record<PondId, PondInfo> = {};
  const triggers: Record<PondId, TriggerView> = {};

  for (const p of payload.ponds) {
    ponds[p.name] = { id: p.name, name: p.name, kind: p.kind, sources: [] };
    pondInfo[p.name] = { version: p.version, kind: p.kind };
    pondViews[p.name] = nodeView(p, p.d_ms);
    if (p.trigger) triggers[p.name] = { kind: p.trigger.kind, boundMs: p.trigger.bound_ms };

    for (const r of p.ripples) {
      const eid = `${p.name}.${r.name}`;
      ripples[eid] = { id: eid, pondId: p.name, name: r.name, parents: [] };
      rippleViews[eid] = nodeView(r, 0);
    }
    // ripple_edges are [sourceName, sinkName] within the Pond → sink.parents includes source.
    for (const [src, snk] of p.ripple_edges) {
      ripples[`${p.name}.${snk}`]?.parents.push(`${p.name}.${src}`);
    }
  }
  // Pond sources from inter-Pond edges [sourcePond, sinkPond].
  for (const [src, snk] of payload.edges) {
    ponds[snk]?.sources.push(src);
  }

  return { ponds, ripples, pondViews, rippleViews, pondInfo, triggers };
}

// ─── Store ───────────────────────────────────────────────────────────────────

export interface RunFilters {
  lineage: boolean; // include the selected Pond's upstream sources (default on)
  ripples: boolean; // nest Ripple Runs under each Pond Run (default off)
}

export interface LiveState extends StatusSlice {
  now: number;
  connected: boolean;
  error: string | null;

  selectedPondId: PondId | null;
  selectedRippleId: RippleId | null;
  selectedTriggerId: PondId | null;

  runs: PondRun[]; // run-history feed (filtered by selection + filters)
  runFilters: RunFilters;
  runLimit: number; // size of the live window the feed fetches; grows as the user scrolls
  runsAtEnd: boolean; // the window reached the oldest available run (fewer rows than asked)
  loadingMore: boolean; // a scroll-triggered window growth is in flight
  selectedPondRuns: PondRun[]; // the selected Pond's own runs (with ripples), for the trace charts
  windowsByPond: Record<PondId, WindowRow[]>;

  refresh(): Promise<void>;
  refreshWindows(pond: PondId): Promise<void>;
  setRunFilter(key: keyof RunFilters, value: boolean): void;
  loadMoreRuns(): void;

  selectPond(id: PondId | null): void;
  selectRipple(id: RippleId | null): void;
  selectTrigger(id: PondId | null): void;
  clearSelection(): void;

  tap(pond: PondId): Promise<void>;
  pulse(pond: PondId): Promise<void>;
  wave(pond: PondId): Promise<void>;
  tide(pond: PondId, boundSeconds: number): Promise<void>;
  start(pond: PondId): Promise<void>;
  stop(pond: PondId, upstream?: boolean): Promise<void>;
  removeTrigger(pond: PondId): Promise<void>;

  addWindow(pond: PondId, body: AddWindowBody): Promise<void>;
  removeWindow(pond: PondId, name: string): Promise<void>;
}

// Run-history feed window: starts at one page, grows by a page per scroll-to-bottom, hard-capped
// (matches the backend /api/runs clamp). The poll always fetches the current window, so payloads
// stay small until the user scrolls deep.
const RUN_PAGE = 100;
const RUN_MAX = 1000;

// The Pond a run-history / chart query should focus on: an explicitly selected Pond, or the Pond
// owning a selected Ripple.
function focusPond(s: LiveState): PondId | null {
  if (s.selectedPondId) return s.selectedPondId;
  if (s.selectedRippleId) return s.ripples[s.selectedRippleId]?.pondId ?? null;
  return null;
}

export const useLiveStore = create<LiveState>((set, get) => ({
  ponds: {},
  ripples: {},
  pondViews: {},
  rippleViews: {},
  pondInfo: {},
  triggers: {},
  now: Date.now(),
  connected: false,
  error: null,

  selectedPondId: null,
  selectedRippleId: null,
  selectedTriggerId: null,

  runs: [],
  runFilters: { lineage: true, ripples: false },
  runLimit: RUN_PAGE,
  runsAtEnd: false,
  loadingMore: false,
  selectedPondRuns: [],
  windowsByPond: {},

  async refresh() {
    try {
      const payload = await fetchStatus();
      set({ ...transformStatus(payload), now: Date.now(), connected: true, error: null });
    } catch (e) {
      set({ connected: false, error: e instanceof Error ? e.message : String(e) });
      return; // if the Catchment is unreachable, skip the dependent fetches this tick
    }

    const s = get();
    const pond = focusPond(s);
    const limit = s.runLimit;
    try {
      const [feed, ownRuns] = await Promise.all([
        fetchRuns({ pond, lineage: s.runFilters.lineage, ripples: s.runFilters.ripples, limit }),
        pond ? fetchRuns({ pond, lineage: false, ripples: true, limit: 60 }) : Promise.resolve([]),
      ]);
      // Fewer rows than the window means we've reached the oldest run (also true once at the cap).
      set({
        runs: feed.map(mapRun),
        selectedPondRuns: ownRuns.map(mapRun),
        runsAtEnd: feed.length < limit || limit >= RUN_MAX,
      });
    } catch {
      /* history is non-critical; leave the last good feed in place */
    }
  },

  async refreshWindows(pond) {
    try {
      const windows = await fetchWindows(pond);
      set((s) => ({ windowsByPond: { ...s.windowsByPond, [pond]: windows.map(mapWindow) } }));
    } catch {
      /* ignore */
    }
  },

  setRunFilter(key, value) {
    // Changing the feed resets the scroll window to the first page.
    set((s) => ({ runFilters: { ...s.runFilters, [key]: value }, runLimit: RUN_PAGE, runsAtEnd: false }));
    get().refresh();
  },

  loadMoreRuns() {
    const s = get();
    if (s.loadingMore || s.runsAtEnd || s.runLimit >= RUN_MAX) return;
    set({ loadingMore: true, runLimit: Math.min(s.runLimit + RUN_PAGE, RUN_MAX) });
    get().refresh().finally(() => set({ loadingMore: false }));
  },

  selectPond(id) {
    set({ selectedPondId: id, selectedRippleId: null, selectedTriggerId: null, selectedPondRuns: [], runLimit: RUN_PAGE, runsAtEnd: false });
    if (id && get().ponds[id]?.sources.length === 0) get().refreshWindows(id);
    get().refresh();
  },
  selectRipple(id) {
    const pondId = id ? get().ripples[id]?.pondId ?? null : null;
    set({ selectedRippleId: id, selectedPondId: pondId, selectedTriggerId: null, selectedPondRuns: [], runLimit: RUN_PAGE, runsAtEnd: false });
    get().refresh();
  },
  selectTrigger(id) {
    set({ selectedTriggerId: id, selectedPondId: null, selectedRippleId: null });
  },
  clearSelection() {
    set({ selectedPondId: null, selectedRippleId: null, selectedTriggerId: null, selectedPondRuns: [], runLimit: RUN_PAGE, runsAtEnd: false });
  },

  tap: (pond) => act(get, set, () => postTrigger(pond, 'tap')),
  pulse: (pond) => act(get, set, () => postTrigger(pond, 'pulse')),
  wave: (pond) => act(get, set, () => postTrigger(pond, 'wave')),
  tide: (pond, boundSeconds) => act(get, set, () => postTrigger(pond, 'tide', { bound_seconds: boundSeconds })),
  start: (pond) => act(get, set, () => postTrigger(pond, 'start')),
  stop: (pond, upstream = false) => act(get, set, () => postTrigger(pond, 'stop', { upstream })),
  removeTrigger: (pond) => act(get, set, () => postTrigger(pond, 'untrigger')),

  addWindow: (pond, body) =>
    act(get, set, async () => {
      await addWindow(pond, body);
      await get().refreshWindows(pond);
    }),
  removeWindow: (pond, name) =>
    act(get, set, async () => {
      await removeWindow(pond, name);
      await get().refreshWindows(pond);
    }),
}));

// Run a control action, surface any error, and immediately re-poll so the UI reflects the result
// without waiting for the next tick.
async function act(
  get: () => LiveState,
  set: (partial: Partial<LiveState>) => void,
  fn: () => Promise<void>,
): Promise<void> {
  try {
    await fn();
    set({ error: null });
    await get().refresh();
  } catch (e) {
    set({ error: e instanceof Error ? e.message : String(e) });
  }
}

// ─── Visual helpers (shared by the node components) ──────────────────────────

// Age of a freshness timestamp relative to now, unit-scaled. F=0 (NEVER) → '—'. Two-digit numeric
// part so widths don't jitter as a value crosses 9↔10.
export function formatAge(F: number, now: number): string {
  if (!F) return '—';
  const pad = (n: number) => String(Math.floor(n)).padStart(2, '0');
  const secs = Math.max(0, (now - F) / 1000);
  if (secs < 60) return `${pad(secs)}s`;
  const mins = secs / 60;
  if (mins < 60) return `${pad(mins)}m`;
  const hrs = mins / 60;
  if (hrs < 24) return `${pad(hrs)}h`;
  const days = hrs / 24;
  if (days < 30) return `${pad(days)}d`;
  const months = days / 30;
  if (months < 12) return `${pad(months)}mo`;
  return '>1y';
}

// A staleness bound (ms) as a compact duration in its largest whole unit: 86_400_000 → "1d",
// 5_400_000 → "1.5h", 2_000 → "2s".
export function formatDuration(ms: number): string {
  const s = ms / 1000;
  for (const [label, unit] of [['w', 604800], ['d', 86400], ['h', 3600], ['m', 60], ['s', 1]] as const) {
    if (s >= unit) {
      const v = s / unit;
      return Number.isInteger(v) ? `${v}${label}` : `${v.toFixed(1)}${label}`;
    }
  }
  return `${s}s`;
}

// ─── Semantic palette ────────────────────────────────────────────────────────
// Named by role, not hue, so the values can move without the names lying. pull / push / running form
// a roughly-even triad (they are routinely co-visible and must mutually contrast); success/danger are
// the fixed green/red conventions. THEME_BRAND (the Duckstring colour) IS the running colour and also
// the generic accent.
export const THEME_BRAND = '#06c4e6'; // a pond on a bright day — full-saturation water cyan
export const THEME_RUNNING = THEME_BRAND; // active execution + brand accent
export const THEME_PULL = '#ee9333'; // pull (Tap/Wave) · queued · run interval — warm amber-orange
export const THEME_PUSH = '#a3e635'; // push (Pulse/Tide) — green-yellow
export const THEME_SUCCESS = '#22c55e'; // success · connected · start (green)
export const THEME_DANGER = '#ef4444'; // stop · failed (red)

// Node/Ripple demand state → border colour.
export const STATE_COLORS: Record<string, string> = {
  running: THEME_RUNNING,
  queued: THEME_PULL,
  idle: '#71717a',
};

// Border colour for a node. Running is always the brand cyan and idle is grey; a *queued* node is
// coloured by its demand — push if it holds any push target, else pull. (Push is the more specific
// demand, so it wins the colour when a node holds both.)
export function stateColor(view: NodeView): string {
  if (view.status === 'queued') {
    if (view.targetF !== null) return THEME_PUSH;
    if (view.hasPull) return THEME_PULL;
  }
  return STATE_COLORS[view.status] ?? STATE_COLORS.idle;
}

// pull amber; push green-yellow; idle grey.
export const EDGE_COLORS: Record<string, string> = {
  pull: THEME_PULL,
  push: THEME_PUSH,
  idle: '#3f3f46',
};

// Edge colour = whether the child can consume the parent. Coloured when the parent's output is
// fresher than the child's last run start (parent.endF > child.startF); push if consuming it would
// also meet a push target the child holds (parent.endF >= child.targetF), else pull. Freshnesses are
// ms-epoch (0 = never); childTargetF is null when there is no push target.
export function consumeEdgeColor(parentEndF: number, childStartF: number, childTargetF: number | null): string {
  if (parentEndF <= childStartF) return EDGE_COLORS.idle;
  if (childTargetF !== null && parentEndF >= childTargetF) return EDGE_COLORS.push;
  return EDGE_COLORS.pull;
}

// Internal fill of a node/pill: its rim colour washed in at low alpha over the dark canvas, so the
// interior is a dark shade of the rim. Shared by Ponds, Ripples, and the trigger pills so they match.
const FILL_ALPHA = '17'; // ~9% — tune to taste
export function nodeFill(rim: string): string {
  return `${rim}${FILL_ALPHA}`;
}
