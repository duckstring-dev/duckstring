import { create } from 'zustand';
import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  PondVisualState,
  RippleVisualState,
  Refresh,
} from './types';
import type { Window, LogEntry } from './types';
import { hasCyclePond, hasCycleRipple, isPondDownstreamOf } from './graph';
import { tick as orchTick, refreshNow as orchRefreshNow, drainLog, type OrchestrState } from './orchestration';

const MAX_LOGS = 2000;

let pondCounter = 0;
const rippleCounters: Record<PondId, number> = {};

function newPondId(): PondId {
  return `pond-${++pondCounter}`;
}

function newPondName(): string {
  return `p${pondCounter}`;
}

function newRippleId(): RippleId {
  return `ripple-${Math.random().toString(36).slice(2, 9)}`;
}

function newRippleName(pondId: PondId): string {
  rippleCounters[pondId] = (rippleCounters[pondId] ?? 0) + 1;
  return `r${rippleCounters[pondId]}`;
}

function initialPondState(): PondRunState {
  return {
    startF: 0,
    endF: 0,
    D: 0,
    targets: [],
    isKilled: false,
    isBlocked: false,
    runsStarted: 0,
    runsCompleted: 0,
    startTimesByF: {},
    completionTimes: [],
    durations: [],
  };
}

function initialRippleState(): RippleRunState {
  return {
    startF: 0,
    endF: 0,
    targets: [],
    isRunning: false,
    runStartedAt: null,
    currentRunDurationMs: null,
    lastDurationMs: null,
    runsStarted: 0,
    runsCompleted: 0,
    completionTimes: [],
    durations: [],
  };
}

function buildDemoState(): Pick<PlaygroundState, 'ponds' | 'pondStates' | 'ripples' | 'rippleStates'> {
  const txId = newPondId();
  const prodId = newPondId();
  const salesId = newPondId();
  const reportsId = newPondId();

  const txIngestId = newRippleId();
  const prodIngestId = newRippleId();
  const dailySalesId = newRippleId();
  const priceTiersId = newRippleId();
  const joinLinesId = newRippleId();
  const monthlySummaryId = newRippleId();

  rippleCounters[txId] = 1;
  rippleCounters[prodId] = 1;
  rippleCounters[salesId] = 3;
  rippleCounters[reportsId] = 1;

  return {
    ponds: {
      [txId]: { id: txId, name: 'transactions', sources: [] },
      [prodId]: { id: prodId, name: 'products', sources: [] },
      [salesId]: { id: salesId, name: 'sales', sources: [txId, prodId] },
      [reportsId]: { id: reportsId, name: 'reports', sources: [salesId] },
    },
    pondStates: {
      [txId]: initialPondState(),
      [prodId]: initialPondState(),
      [salesId]: initialPondState(),
      [reportsId]: initialPondState(),
    },
    ripples: {
      [txIngestId]: { id: txIngestId, pondId: txId, name: 'ingest', parents: [], durationMs: 500, variability: 0 },
      [prodIngestId]: { id: prodIngestId, pondId: prodId, name: 'ingest', parents: [], durationMs: 500, variability: 0 },
      [dailySalesId]: { id: dailySalesId, pondId: salesId, name: 'daily_sales', parents: [], durationMs: 2000, variability: 0 },
      [priceTiersId]: { id: priceTiersId, pondId: salesId, name: 'price_tiers', parents: [], durationMs: 1000, variability: 0 },
      [joinLinesId]: { id: joinLinesId, pondId: salesId, name: 'join_lines', parents: [dailySalesId, priceTiersId], durationMs: 3000, variability: 0 },
      [monthlySummaryId]: { id: monthlySummaryId, pondId: reportsId, name: 'monthly_summary', parents: [], durationMs: 1000, variability: 0 },
    },
    rippleStates: {
      [txIngestId]: initialRippleState(),
      [prodIngestId]: initialRippleState(),
      [dailySalesId]: initialRippleState(),
      [priceTiersId]: initialRippleState(),
      [joinLinesId]: initialRippleState(),
      [monthlySummaryId]: initialRippleState(),
    },
  };
}

// ─── Example graphs ──────────────────────────────────────────────────────────
// Topologies for exploring how Refreshes are paced, matching the ones it was simulated on. Each Pond lists its
// Sources by name and its Ripples as [name, seconds] (a single Ripple unless noted).

interface ExampleSpec {
  label: string;
  ponds: { name: string; sources?: string[]; ripples: [string, number][] }[];
}

export const EXAMPLES: Record<string, ExampleSpec> = {
  deep: {
    label: 'Deep chain (fast stages, then a 5s bottleneck)',
    ponds: [
      { name: 'inlet', ripples: [['ingest', 1]] },
      { name: 'p1', sources: ['inlet'], ripples: [['r1', 1]] },
      { name: 'p2', sources: ['p1'], ripples: [['r1', 1]] },
      { name: 'p3', sources: ['p2'], ripples: [['r1', 1]] },
      { name: 'p4', sources: ['p3'], ripples: [['r1', 1]] },
      { name: 'bottleneck', sources: ['p4'], ripples: [['slow', 5]] },
      { name: 'outlet', sources: ['bottleneck'], ripples: [['r1', 1]] },
    ],
  },
  decreasing: {
    label: 'Decreasing chain (1s → 3s → 2s → 1s)',
    ponds: [
      { name: 'a', ripples: [['r1', 1]] },
      { name: 'b', sources: ['a'], ripples: [['r1', 3]] },
      { name: 'c', sources: ['b'], ripples: [['r1', 2]] },
      { name: 'd', sources: ['c'], ripples: [['r1', 1]] },
    ],
  },
  sibling: {
    label: 'Slow sibling (a fast and a 6s Inlet feed one Pond)',
    ponds: [
      { name: 'fast', ripples: [['ingest', 1]] },
      { name: 'slow', ripples: [['ingest', 6]] },
      { name: 'join', sources: ['fast', 'slow'], ripples: [['r1', 1]] },
      { name: 'outlet', sources: ['join'], ripples: [['r1', 1]] },
    ],
  },
  diamond: {
    label: 'Diamond (5s bottleneck on one path)',
    ponds: [
      { name: 'a', ripples: [['ingest', 1]] },
      { name: 'b', sources: ['a'], ripples: [['slow', 5]] },
      { name: 'd', sources: ['a'], ripples: [['r1', 1]] },
      { name: 'c', sources: ['b', 'd'], ripples: [['r1', 1]] },
    ],
  },
  diamondlead: {
    label: 'Diamond, long lead on the slow path (five 1s stages, then 3s)',
    ponds: [
      { name: 'a', ripples: [['ingest', 1]] },
      ...[1, 2, 3, 4, 5].map((i) => ({ name: `l${i}`, sources: [i === 1 ? 'a' : `l${i - 1}`], ripples: [['r1', 1]] as [string, number][] })),
      { name: 'b', sources: ['l5'], ripples: [['slow', 3]] },
      { name: 'd', sources: ['a'], ripples: [['r1', 1]] },
      { name: 'c', sources: ['b', 'd'], ripples: [['r1', 1]] },
    ],
  },
  long: {
    label: 'Long lead (seven 1s stages, then a 3s bottleneck)',
    ponds: [
      { name: 'inlet', ripples: [['ingest', 1]] },
      ...[1, 2, 3, 4, 5, 6].map((i) => ({ name: `p${i}`, sources: [i === 1 ? 'inlet' : `p${i - 1}`], ripples: [['r1', 1]] as [string, number][] })),
      { name: 'bottleneck', sources: ['p6'], ripples: [['slow', 3]] },
      { name: 'outlet', sources: ['bottleneck'], ripples: [['r1', 1]] },
    ],
  },
};

function buildExampleState(spec: ExampleSpec): Pick<PlaygroundState, 'ponds' | 'pondStates' | 'ripples' | 'rippleStates'> {
  const ids: Record<string, PondId> = {};
  const out: Pick<PlaygroundState, 'ponds' | 'pondStates' | 'ripples' | 'rippleStates'> = {
    ponds: {},
    pondStates: {},
    ripples: {},
    rippleStates: {},
  };
  for (const p of spec.ponds) {
    const id = newPondId();
    ids[p.name] = id;
    out.ponds[id] = { id, name: p.name, sources: (p.sources ?? []).map((n) => ids[n]) };
    out.pondStates[id] = initialPondState();
    rippleCounters[id] = p.ripples.length;
    let prev: RippleId | null = null;
    for (const [name, secs] of p.ripples) {
      const rid = newRippleId();
      out.ripples[rid] = { id: rid, pondId: id, name, parents: prev ? [prev] : [], durationMs: secs * 1000, variability: 0 };
      out.rippleStates[rid] = initialRippleState();
      prev = rid;
    }
  }
  return out;
}

const demoState = buildDemoState();

// A name-based fingerprint of the graph a visitor can edit (Ponds, Sources, Ripples, parents, durations,
// variability, windows), so "has the visitor changed the example?" doesn't depend on generated ids.
export function graphSignature(s: Pick<PlaygroundState, 'ponds' | 'ripples'>): string {
  const pn = (id: PondId) => s.ponds[id]?.name ?? id;
  const rn = (id: RippleId) => s.ripples[id]?.name ?? id;
  const ponds = Object.values(s.ponds)
    .map((p) => `${p.name}@${p.major ?? 1}<${p.sources.map(pn).sort().join(',')}>${JSON.stringify(p.windows ?? [])}`)
    .sort();
  const ripples = Object.values(s.ripples)
    .map((r) => `${pn(r.pondId)}.${r.name}<${r.parents.map(rn).sort().join(',')}>${r.durationMs}~${r.variability}`)
    .sort();
  return `${ponds.join('|')}#${ripples.join('|')}`;
}

const DEMO_SIGNATURE = graphSignature(demoState);

export interface PlaygroundState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  now: number;
  speed: number;
  paused: boolean;
  logs: LogEntry[];
  selectedPondId: PondId | null;
  selectedRippleId: RippleId | null;
  refreshes: Record<PondId, Refresh[]>;
  // A one-shot Refresh's tag on its Pond, until the Pond completes the Run that serves it.
  refreshTags: Record<PondId, number>;
  // Mobile bottom sheet: open/closed. Selecting a node opens it; the tour also sets it directly.
  sheetOpen: boolean;
  // Bumped when something other than the inputs themselves changes a value they show (the tour, a
  // reset), so the uncontrolled sidebar inputs remount with the new value.
  formEpoch: number;

  // Adds a Pond with one 1 s Ripple and selects it. With no options it links as a Sink of the
  // selected Pond (the sidebar's behaviour); `source` names the Source explicitly. Returns its id.
  addPond(opts?: { name?: string; rippleName?: string; source?: PondId }): PondId;
  // A new major version of a Pond: a copy of its Ripples with the same Sources and nobody reading it. Returns its id.
  addMajorVersion(pondId: PondId, sources?: PondId[]): PondId; // sources default to the copied Pond's
  addRipple(pondId: PondId, parentId?: RippleId): void;
  renamePond(pondId: PondId, name: string): void;
  setPondWindows(pondId: PondId, windows: Window[]): void;
  setRippleDuration(rippleId: RippleId, ms: number): void;
  renameRipple(rippleId: RippleId, name: string): void;
  setRippleVariability(rippleId: RippleId, variability: number): void;
  setAllVariability(variability: number): void;
  deletePond(pondId: PondId): void;
  deleteRipple(rippleId: RippleId): void;
  linkPonds(sourcePondId: PondId, sinkPondId: PondId): boolean;
  unlinkPonds(sourcePondId: PondId, sinkPondId: PondId): void;
  linkRipples(parentId: RippleId, childId: RippleId): boolean;
  unlinkRipples(parentId: RippleId, childId: RippleId): void;

  selectPond(pondId: PondId | null): void;
  selectRipple(rippleId: RippleId | null): void;
  clearSelection(): void;

  // Refresh now: one request for data as fresh as the current moment.
  refreshNow(pondId: PondId): void;
  // Set the Pond's standing Refresh (Outlets only), replacing any it had; null removes it. The engine takes a list
  // per Pond (the Catchment allows several); the playground keeps one.
  setRefresh(pondId: PondId, refresh: Omit<Refresh, 'name'> | null): void;
  // Move the standing Refresh from one Pond to another (an upgrade moving its Refresh to the new major).
  moveRefresh(fromPondId: PondId, toPondId: PondId): void;

  clearLogs(): void;

  // Rebuild the example pipeline and clear triggers, logs and selection.
  resetToDemo(): void;
  // Replace the graph with one of EXAMPLES (or the demo for 'demo'), clearing run state and triggers.
  loadExample(key: string): void;
  // Whether the graph still matches the example (ignoring run state and triggers).
  isDemoGraph(): boolean;
  setSheetOpen(open: boolean): void;
  bumpFormEpoch(): void;

  setSpeed(speed: number): void;
  togglePause(): void;
  tick(now: number): void;
}

function toOrchestrState(state: PlaygroundState): OrchestrState {
  return {
    ponds: state.ponds,
    pondStates: state.pondStates,
    ripples: state.ripples,
    rippleStates: state.rippleStates,
    refreshes: state.refreshes,
  };
}

function applyOrch(prev: PlaygroundState, orch: OrchestrState): Partial<PlaygroundState> {
  const drained = drainLog();
  const logs = drained.length
    ? [...prev.logs, ...drained].slice(-MAX_LOGS)
    : prev.logs;
  return {
    pondStates: orch.pondStates,
    rippleStates: orch.rippleStates,
    logs,
  };
}

function isOutlet(ponds: Record<PondId, Pond>, pondId: PondId): boolean {
  return !Object.values(ponds).some((p) => isPondDownstreamOf(ponds, p.id, pondId) && p.id !== pondId);
}

// A Pond's display name: its name, plus its major version when it isn't the only major of that name (or isn't 1).
export function pondLabel(ponds: Record<PondId, Pond>, pondId: PondId): string {
  const p = ponds[pondId];
  if (!p) return pondId;
  const major = p.major ?? 1;
  const siblings = Object.values(ponds).some((q) => q.id !== pondId && q.name === p.name);
  return siblings || major !== 1 ? `${p.name} v${major}` : p.name;
}

// A short label for a standing Refresh: "Continuous", "Every 4 s", "Every 1 d from 06:00".
export function refreshLabel(r: Omit<Refresh, 'name'>): string {
  if (r.startAnchor == null && r.everyMs <= 0) return 'Continuous';
  const every = `Every ${formatDuration(r.everyMs).replace(/([a-z]+)$/, ' $1')}`;
  if (r.startAnchor == null) return every;
  const d = new Date(r.startAnchor);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = d.getSeconds() ? `:${String(d.getSeconds()).padStart(2, '0')}` : '';
  return `${every} from ${hh}:${mm}${ss}`;
}

export const usePlaygroundStore = create<PlaygroundState>((set, get) => ({
  now: Date.now(),
  speed: 1,
  paused: false,
  ponds: demoState.ponds,
  pondStates: demoState.pondStates,
  ripples: demoState.ripples,
  rippleStates: demoState.rippleStates,
  logs: [],
  selectedPondId: null,
  selectedRippleId: null,
  refreshes: {},
  refreshTags: {},
  sheetOpen: false,
  formEpoch: 0,

  addPond(opts) {
    // If a pond is selected, link the new pond as its sink.
    const sourcePondId = opts ? opts.source : get().selectedPondId;
    const id = newPondId();
    const pond: Pond = { id, name: opts?.name ?? newPondName(), sources: [] };
    rippleCounters[id] = 0;
    const rippleId = newRippleId();
    const rippleName = newRippleName(id);
    const ripple: Ripple = {
      id: rippleId,
      pondId: id,
      name: opts?.rippleName ?? rippleName,
      parents: [],
      durationMs: 1000,
      variability: 0,
    };
    set((s) => ({
      ponds: { ...s.ponds, [id]: pond },
      pondStates: { ...s.pondStates, [id]: initialPondState() },
      ripples: { ...s.ripples, [rippleId]: ripple },
      rippleStates: { ...s.rippleStates, [rippleId]: initialRippleState() },
      selectedPondId: id,
      selectedRippleId: null,
    }));
    if (sourcePondId && get().ponds[sourcePondId]) {
      get().linkPonds(sourcePondId, id);
    }
    return id;
  },

  addMajorVersion(pondId, sources) {
    const s = get();
    const orig = s.ponds[pondId];
    const id = newPondId();
    const major = Math.max(...Object.values(s.ponds).filter((p) => p.name === orig.name).map((p) => p.major ?? 1)) + 1;
    const own = Object.values(s.ripples).filter((r) => r.pondId === pondId);
    const ids: Record<RippleId, RippleId> = Object.fromEntries(own.map((r) => [r.id, newRippleId()]));
    const ripples: Record<RippleId, Ripple> = {};
    const rippleStates: Record<RippleId, RippleRunState> = {};
    for (const r of own) {
      ripples[ids[r.id]] = { ...r, id: ids[r.id], pondId: id, parents: r.parents.map((p) => ids[p] ?? p) };
      rippleStates[ids[r.id]] = initialRippleState();
    }
    rippleCounters[id] = rippleCounters[pondId] ?? own.length;
    set((st) => ({
      ponds: {
        ...st.ponds,
        [id]: {
          id,
          name: orig.name,
          major,
          sources: [...(sources ?? orig.sources)],
          optionalSources: orig.optionalSources?.filter((o) => (sources ?? orig.sources).includes(o)),
          windows: orig.windows,
        },
      },
      pondStates: { ...st.pondStates, [id]: initialPondState() },
      ripples: { ...st.ripples, ...ripples },
      rippleStates: { ...st.rippleStates, ...rippleStates },
      selectedPondId: id,
      selectedRippleId: null,
    }));
    return id;
  },

  addRipple(pondId, parentId) {
    const rippleId = newRippleId();
    const ripple: Ripple = {
      id: rippleId,
      pondId,
      name: newRippleName(pondId),
      parents: [],
      durationMs: 1000,
      variability: 0,
    };
    set((s) => ({
      ripples: { ...s.ripples, [rippleId]: ripple },
      rippleStates: { ...s.rippleStates, [rippleId]: initialRippleState() },
    }));
    if (parentId && get().ripples[parentId]?.pondId === pondId) {
      get().linkRipples(parentId, rippleId);
    }
  },

  setRippleDuration(rippleId, ms) {
    set((s) => ({
      ripples: { ...s.ripples, [rippleId]: { ...s.ripples[rippleId], durationMs: ms } },
    }));
  },

  renamePond(pondId, name) {
    set((s) => ({
      ponds: { ...s.ponds, [pondId]: { ...s.ponds[pondId], name } },
    }));
  },

  setPondWindows(pondId, windows) {
    set((s) => ({
      ponds: { ...s.ponds, [pondId]: { ...s.ponds[pondId], windows: windows.length ? windows : undefined } },
    }));
  },

  renameRipple(rippleId, name) {
    set((s) => ({
      ripples: { ...s.ripples, [rippleId]: { ...s.ripples[rippleId], name } },
    }));
  },

  setRippleVariability(rippleId, variability) {
    set((s) => ({
      ripples: { ...s.ripples, [rippleId]: { ...s.ripples[rippleId], variability } },
    }));
  },

  setAllVariability(variability) {
    set((s) => {
      const ripples: Record<RippleId, Ripple> = {};
      for (const [id, r] of Object.entries(s.ripples)) ripples[id] = { ...r, variability };
      return { ripples };
    });
  },

  deletePond(pondId) {
    set((s) => {
      const refreshes = { ...s.refreshes };
      delete refreshes[pondId];
      return { refreshes };
    });
    const pondRippleIds = new Set(
      Object.values(get().ripples).filter((r) => r.pondId === pondId).map((r) => r.id)
    );
    set((s) => {
      const newPonds: typeof s.ponds = {};
      for (const [id, p] of Object.entries(s.ponds)) {
        if (id === pondId) continue;
        newPonds[id] = p.sources.includes(pondId)
          ? { ...p, sources: p.sources.filter((sid) => sid !== pondId) }
          : p;
      }
      const newPondStates = { ...s.pondStates };
      delete newPondStates[pondId];
      const newRipples: typeof s.ripples = {};
      const newRippleStates: typeof s.rippleStates = {};
      for (const [id, r] of Object.entries(s.ripples)) {
        if (!pondRippleIds.has(id)) {
          newRipples[id] = r;
          newRippleStates[id] = s.rippleStates[id];
        }
      }
      return {
        ponds: newPonds,
        pondStates: newPondStates,
        ripples: newRipples,
        rippleStates: newRippleStates,
        selectedPondId: s.selectedPondId === pondId ? null : s.selectedPondId,
        selectedRippleId: pondRippleIds.has(s.selectedRippleId ?? '') ? null : s.selectedRippleId,
      };
    });
  },

  deleteRipple(rippleId) {
    set((s) => {
      const newRipples: typeof s.ripples = {};
      for (const [id, r] of Object.entries(s.ripples)) {
        if (id === rippleId) continue;
        newRipples[id] = r.parents.includes(rippleId)
          ? { ...r, parents: r.parents.filter((pid) => pid !== rippleId) }
          : r;
      }
      const newRippleStates = { ...s.rippleStates };
      delete newRippleStates[rippleId];
      return {
        ripples: newRipples,
        rippleStates: newRippleStates,
        selectedRippleId: s.selectedRippleId === rippleId ? null : s.selectedRippleId,
      };
    });
  },

  linkPonds(sourcePondId, sinkPondId) {
    const state = get();
    if (sourcePondId === sinkPondId) return false;
    const sink = state.ponds[sinkPondId];
    if (!sink) return false;
    if (sink.sources.includes(sourcePondId)) return false;
    if (hasCyclePond(state.ponds, sourcePondId, sinkPondId)) return false;
    set((s) => ({
      ponds: {
        ...s.ponds,
        [sinkPondId]: { ...s.ponds[sinkPondId], sources: [...s.ponds[sinkPondId].sources, sourcePondId] },
      },
    }));
    // Refreshes live only on outlets. Linking can demote a pond to non-outlet: drop its Refreshes (requests already
    // made are served; nothing new is asked for).
    const ponds = get().ponds;
    const kept = Object.fromEntries(Object.entries(get().refreshes).filter(([pid]) => isOutlet(ponds, pid)));
    set({ refreshes: kept });
    return true;
  },

  unlinkPonds(sourcePondId, sinkPondId) {
    set((s) => ({
      ponds: {
        ...s.ponds,
        [sinkPondId]: {
          ...s.ponds[sinkPondId],
          sources: s.ponds[sinkPondId].sources.filter((id) => id !== sourcePondId),
        },
      },
    }));
  },

  linkRipples(parentId, childId) {
    const state = get();
    if (parentId === childId) return false;
    const parent = state.ripples[parentId];
    const child = state.ripples[childId];
    if (!parent || !child) return false;
    if (parent.pondId !== child.pondId) return false;
    if (child.parents.includes(parentId)) return false;
    if (hasCycleRipple(state.ripples, parentId, childId)) return false;
    set((s) => ({
      ripples: {
        ...s.ripples,
        [childId]: { ...s.ripples[childId], parents: [...s.ripples[childId].parents, parentId] },
      },
    }));
    return true;
  },

  unlinkRipples(parentId, childId) {
    set((s) => ({
      ripples: {
        ...s.ripples,
        [childId]: {
          ...s.ripples[childId],
          parents: s.ripples[childId].parents.filter((id) => id !== parentId),
        },
      },
    }));
  },

  selectPond(pondId) {
    set((s) => ({
      selectedPondId: pondId,
      selectedRippleId: null,
      sheetOpen: pondId ? true : s.sheetOpen,
    }));
  },

  selectRipple(rippleId) {
    const ripple = get().ripples[rippleId ?? ''];
    set((s) => ({
      selectedRippleId: rippleId,
      selectedPondId: ripple?.pondId ?? null,
      sheetOpen: rippleId ? true : s.sheetOpen,
    }));
  },

  clearSelection() {
    set({ selectedPondId: null, selectedRippleId: null });
  },

  refreshNow(pondId) {
    set((s) => ({
      ...applyOrch(s, orchRefreshNow(toOrchestrState(s), pondId, s.now)),
      refreshTags: { ...s.refreshTags, [pondId]: s.pondStates[pondId]?.runsCompleted ?? 0 },
    }));
  },

  setRefresh(pondId, refresh) {
    if (refresh && !isOutlet(get().ponds, pondId)) return;
    if (refresh && refresh.startAnchor != null && refresh.everyMs <= 0) return; // a schedule needs a period
    set((s) => {
      const refreshes = { ...s.refreshes };
      if (refresh) refreshes[pondId] = [{ ...refresh, name: refreshLabel(refresh) }];
      else delete refreshes[pondId];
      return { refreshes };
    });
  },

  moveRefresh(fromPondId, toPondId) {
    set((s) => {
      const moving = s.refreshes[fromPondId];
      const refreshes = { ...s.refreshes };
      delete refreshes[fromPondId];
      if (moving?.length) refreshes[toPondId] = moving;
      return { refreshes };
    });
  },

  clearLogs() {
    set({ logs: [] });
  },

  resetToDemo() {
    // Settle the engine's log buffer first, so events from the old graph don't land after the reset.
    drainLog();
    set((s) => ({
      ...buildDemoState(),
      refreshes: {},
      refreshTags: {},
      logs: [],
      selectedPondId: null,
      selectedRippleId: null,
      formEpoch: s.formEpoch + 1,
    }));
  },

  loadExample(key) {
    if (key === 'demo' || !EXAMPLES[key]) {
      get().resetToDemo();
      return;
    }
    drainLog();
    set((s) => ({
      ...buildExampleState(EXAMPLES[key]),
      refreshes: {},
      refreshTags: {},
      logs: [],
      selectedPondId: null,
      selectedRippleId: null,
      formEpoch: s.formEpoch + 1,
    }));
  },

  isDemoGraph() {
    return graphSignature(get()) === DEMO_SIGNATURE;
  },

  setSheetOpen(open) {
    set({ sheetOpen: open });
  },

  bumpFormEpoch() {
    set((s) => ({ formEpoch: s.formEpoch + 1 }));
  },

  setSpeed(speed) {
    set({ speed });
  },

  togglePause() {
    set((s) => ({ paused: !s.paused }));
  },

  tick(now) {
    set((s) => {
      // Advance the engine in fixed SIM_STEP sub-ticks so the simulation's time resolution is the
      // same at every playback speed. At 10x the driver hands us a ~1000ms jump; stepping it as one
      // tick would snap run starts/completions onto a coarse grid and jitter the cadence (3↔4s).
      // MAX_STEPS caps a catch-up after the tab was backgrounded (the remainder collapses into the
      // final tick rather than freezing the UI).
      const SIM_STEP = 100;
      const MAX_STEPS = 256;
      let cur = s.now;
      let orch = toOrchestrState(s);
      let n = 0;
      while (now - cur > SIM_STEP && n < MAX_STEPS) {
        cur += SIM_STEP;
        orch = orchTick(cur, orch);
        n += 1;
      }
      orch = orchTick(now, orch);
      return { ...applyOrch(s, orch), now };
    });
  },
}));

// ─── Visual state helpers ────────────────────────────────────────────────────

// Age of a freshness timestamp relative to now, unit-scaled. F=0 or NEVER (-Inf, e.g. right after a
// Force) → '—'. The numeric part is always two digits (e.g. "02s", "47m") so the width never jitters
// as a value crosses 9↔10; each unit rolls to the next before it would exceed 99. Caps at ">1y".
export function formatAge(F: number, now: number): string {
  if (!F || !Number.isFinite(F)) return '—';
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

// A duration (ms) as a compact value in its largest whole unit: 86_400_000 → "1d", 5_400_000 → "1.5h",
// 2_000 → "2s".
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

// The freshest pending requested freshness (for the ≤ box / edge colour), or null if none are outstanding.
// NEVER (-Inf) targets from a Force are non-finite and excluded — they carry no displayable age.
export function pushTargetF(targets: number[]): number | null {
  const finite = targets.filter((t) => Number.isFinite(t));
  return finite.length ? Math.max(...finite) : null;
}

export function getRippleVisualState(rs: RippleRunState): RippleVisualState {
  if (rs.isRunning) return 'running';
  if (rs.targets.length > 0) return 'queued';
  return 'idle';
}

// Killed/blocked take precedence over demand state, matching the Catchment status string
// (failed, killed, blocked, running, queued, idle; failures are out of the sim's scope).
// `busy` is whether any of the Pond's Ripples is running (the backend's notion of a running Pond).
export function getPondVisualState(ps: PondRunState, busy: boolean): PondVisualState {
  if (ps.isKilled) return 'killed';
  if (ps.isBlocked) return 'blocked';
  if (busy) return 'running';
  if (ps.targets.length > 0) return 'queued';
  return 'idle';
}

// ─── Semantic palette (kept in lockstep with frontend/src/lib/store.ts) ──────
// Named by role, not hue, so the values can move without the names lying. THEME_BRAND (the Duckstring colour)
// is the running colour and the generic accent; success/danger are the fixed green/red conventions.
export const THEME_BRAND = '#06c4e6'; // a pond on a bright day: full-saturation water cyan
export const THEME_RUNNING = THEME_BRAND; // active execution + brand accent
export const THEME_DEMAND = '#a3e635'; // demand: a Refresh, a requested freshness (green-yellow)
export const THEME_AMBER = '#ee9333'; // the throttle and run intervals (warm amber-orange)
export const THEME_SUCCESS = '#22c55e'; // success (green)
export const THEME_DANGER = '#ef4444'; // remove · failed (red)
export const THEME_BLOCKED = '#991b1b'; // blocked by an upstream kill: a darker, muted red

// Node/Ripple state → border colour. Killed/blocked (Ponds) take precedence in the visual state.
export const STATE_COLORS: Record<string, string> = {
  running: THEME_RUNNING,
  queued: THEME_DEMAND,
  idle: '#71717a',
  killed: THEME_DANGER, // killed reads as red, like failed
  blocked: THEME_BLOCKED,
};

// Border colour for a node: running is the brand cyan, idle grey, queued (holding demand) the demand colour.
export function stateColor(status: string): string {
  return STATE_COLORS[status] ?? STATE_COLORS.idle;
}

export const EDGE_COLORS: Record<string, string> = {
  demand: THEME_DEMAND,
  idle: '#3f3f46',
};

// Edge colour: the demand colour when the child holds a request that the parent's newer output would meet
// (parent.endF > child.startF and parent.endF >= the child's requested freshness), else idle. Freshnesses are
// ms-epoch (0 = never); childTargetF is null when the child holds no request.
export function consumeEdgeColor(parentEndF: number, childStartF: number, childTargetF: number | null): string {
  if (parentEndF > childStartF && childTargetF !== null && parentEndF >= childTargetF) return EDGE_COLORS.demand;
  return EDGE_COLORS.idle;
}

// Internal fill of a node/pill: its rim colour washed in at low alpha over the dark canvas, so the
// interior is a dark shade of the rim. Shared by Ponds, Ripples, and the Refresh pills so they match.
const FILL_ALPHA = '17'; // ~9%
export function nodeFill(rim: string): string {
  return `${rim}${FILL_ALPHA}`;
}
