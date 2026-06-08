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

const isoToMs = (iso: string | null): number => (iso ? Date.parse(iso) : 0);
const isoToMsOrNull = (iso: string | null): number | null => (iso ? Date.parse(iso) : null);

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
  selectedPondRuns: PondRun[]; // the selected Pond's own runs (with ripples), for the trace charts
  windowsByPond: Record<PondId, WindowRow[]>;

  refresh(): Promise<void>;
  refreshWindows(pond: PondId): Promise<void>;
  setRunFilter(key: keyof RunFilters, value: boolean): void;

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
    try {
      const [feed, ownRuns] = await Promise.all([
        fetchRuns({ pond, lineage: s.runFilters.lineage, ripples: s.runFilters.ripples, limit: 200 }),
        pond ? fetchRuns({ pond, lineage: false, ripples: true, limit: 60 }) : Promise.resolve([]),
      ]);
      set({ runs: feed.map(mapRun), selectedPondRuns: ownRuns.map(mapRun) });
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
    set((s) => ({ runFilters: { ...s.runFilters, [key]: value } }));
    get().refresh();
  },

  selectPond(id) {
    set({ selectedPondId: id, selectedRippleId: null, selectedTriggerId: null, selectedPondRuns: [] });
    if (id && get().ponds[id]?.sources.length === 0) get().refreshWindows(id);
    get().refresh();
  },
  selectRipple(id) {
    const pondId = id ? get().ripples[id]?.pondId ?? null : null;
    set({ selectedRippleId: id, selectedPondId: pondId, selectedTriggerId: null, selectedPondRuns: [] });
    get().refresh();
  },
  selectTrigger(id) {
    set({ selectedTriggerId: id, selectedPondId: null, selectedRippleId: null });
  },
  clearSelection() {
    set({ selectedPondId: null, selectedRippleId: null, selectedTriggerId: null, selectedPondRuns: [] });
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

export const STATE_COLORS: Record<string, string> = {
  running: '#22c55e',
  queued: '#f97316',
  idle: '#71717a',
};

// pull (Tap/Wave) green; push (Pulse/Tide) blue; idle grey.
export const EDGE_COLORS: Record<string, string> = {
  pull: '#22c55e',
  push: '#3b82f6',
  idle: '#3f3f46',
};

// Edge colour reflects the SINK's demand on this edge: push (blue) > pull (green) > none (grey).
export function getDemandEdgeColor(sinkPull: boolean, sinkPush: number | null): string {
  if (sinkPush !== null) return EDGE_COLORS.push;
  if (sinkPull) return EDGE_COLORS.pull;
  return EDGE_COLORS.idle;
}
