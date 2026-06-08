import { create } from 'zustand';
import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  EdgeKindMap,
  EdgeDemandKind,
  ActiveTrigger,
} from './types';
import type { Window, LogEntry } from './types';
import { hasCyclePond, hasCycleRipple, isPondDownstreamOf } from './graph';
import {
  tick as orchTick,
  tapPond,
  pulsePond,
  stopPond as orchStopPond,
  startPond as orchStartPond,
  drainLog,
  type OrchestrState,
} from './orchestration';

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
    hasReceivedPull: false,
    hasPull: false,
    targets: [],
    runsStarted: 0,
    runsCompleted: 0,
    genStartTimes: {},
    completionTimes: [],
    durations: [],
  };
}

function initialRippleState(): RippleRunState {
  return {
    startF: 0,
    endF: 0,
    hasPull: false,
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
      [txIngestId]: { id: txIngestId, pondId: txId, name: 'ingest', parents: [], durationMs: 1000, variability: 0 },
      [prodIngestId]: { id: prodIngestId, pondId: prodId, name: 'ingest', parents: [], durationMs: 2000, variability: 0 },
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

const demoState = buildDemoState();

export interface PlaygroundState {
  ponds: Record<PondId, Pond>;
  pondStates: Record<PondId, PondRunState>;
  ripples: Record<RippleId, Ripple>;
  rippleStates: Record<RippleId, RippleRunState>;
  edgeKinds: EdgeKindMap;
  now: number;
  speed: number;
  paused: boolean;
  logs: LogEntry[];
  selectedPondId: PondId | null;
  selectedRippleId: RippleId | null;
  selectedTriggerId: PondId | null;
  triggers: Record<PondId, ActiveTrigger>;
  pulseTags: Record<PondId, number>;

  addPond(): void;
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
  selectTrigger(pondId: PondId | null): void;
  clearSelection(): void;

  triggerTap(pondId: PondId): void;
  triggerPulse(pondId: PondId): void;
  triggerWave(pondId: PondId): void;
  triggerTide(pondId: PondId, stalenessMs: number): void;
  triggerStart(pondId: PondId): void;
  triggerStop(pondId: PondId, upstream?: boolean): void;
  removeTrigger(pondId: PondId): void;
  clearLogs(): void;

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
    edgeKinds: state.edgeKinds,
    triggers: state.triggers,
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
    edgeKinds: orch.edgeKinds,
    triggers: orch.triggers,
    logs,
  };
}

function isOutlet(ponds: Record<PondId, Pond>, pondId: PondId): boolean {
  return !Object.values(ponds).some((p) => isPondDownstreamOf(ponds, p.id, pondId) && p.id !== pondId);
}

export const usePlaygroundStore = create<PlaygroundState>((set, get) => ({
  now: Date.now(),
  speed: 1,
  paused: false,
  ponds: demoState.ponds,
  pondStates: demoState.pondStates,
  ripples: demoState.ripples,
  rippleStates: demoState.rippleStates,
  edgeKinds: {},
  logs: [],
  selectedPondId: null,
  selectedRippleId: null,
  selectedTriggerId: null,
  triggers: {},
  pulseTags: {},

  addPond() {
    // If a pond is selected, link the new pond as its sink.
    const sourcePondId = get().selectedPondId;
    const id = newPondId();
    const pond: Pond = { id, name: newPondName(), sources: [] };
    rippleCounters[id] = 0;
    const rippleId = newRippleId();
    const ripple: Ripple = {
      id: rippleId,
      pondId: id,
      name: newRippleName(id),
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
      selectedTriggerId: null,
    }));
    if (sourcePondId && get().ponds[sourcePondId]) {
      get().linkPonds(sourcePondId, id);
    }
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
    get().removeTrigger(pondId);
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
        selectedTriggerId: s.selectedTriggerId === pondId ? null : s.selectedTriggerId,
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
    // Triggers live only on outlets. Linking can demote a pond to non-outlet;
    // drop its trigger so its wave peters out naturally (no stop signal sent).
    const ponds = get().ponds;
    for (const pid of Object.keys(get().triggers)) {
      if (!isOutlet(ponds, pid)) get().removeTrigger(pid);
    }
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
    set({ selectedPondId: pondId, selectedRippleId: null, selectedTriggerId: null });
  },

  selectRipple(rippleId) {
    const ripple = get().ripples[rippleId ?? ''];
    set({
      selectedRippleId: rippleId,
      selectedPondId: ripple?.pondId ?? null,
      selectedTriggerId: null,
    });
  },

  selectTrigger(pondId) {
    set({ selectedTriggerId: pondId, selectedRippleId: null, selectedPondId: null });
  },

  clearSelection() {
    set({ selectedPondId: null, selectedRippleId: null, selectedTriggerId: null });
  },

  // Tap: one-shot resupply (pull). Cascades synchronously. Stamped from the sim clock.
  triggerTap(pondId) {
    set((s) => applyOrch(s, tapPond(toOrchestrState(s), pondId, s.now)));
  },

  // Pulse: one-shot priority freshness target (push to sim-now). Cascades synchronously.
  triggerPulse(pondId) {
    set((s) => ({
      ...applyOrch(s, pulsePond(toOrchestrState(s), pondId, s.now)),
      pulseTags: { ...s.pulseTags, [pondId]: s.pondStates[pondId]?.runsCompleted ?? 0 },
    }));
  },

  // Wave: a standing pull, re-asserted by orchestration.tick on each Pond completion.
  triggerWave(pondId) {
    if (!isOutlet(get().ponds, pondId)) return;
    set((s) => ({ triggers: { ...s.triggers, [pondId]: { pondId, kind: 'wave' } } }));
  },

  // Tide: maintain a maximum staleness, evaluated by orchestration.tick.
  triggerTide(pondId, stalenessMs) {
    set((s) => ({ triggers: { ...s.triggers, [pondId]: { pondId, kind: 'tide', stalenessMs } } }));
  },

  // Start: inject a one-off run on the Pond alone (push target NEVER), no upstream propagation.
  triggerStart(pondId) {
    set((s) => applyOrch(s, orchStartPond(toOrchestrState(s), pondId, s.now)));
  },

  triggerStop(pondId, upstream = false) {
    get().removeTrigger(pondId);
    set((s) => applyOrch(s, orchStopPond(toOrchestrState(s), pondId, s.now, upstream)));
  },

  removeTrigger(pondId) {
    set((s) => {
      const newTriggers = { ...s.triggers };
      delete newTriggers[pondId];
      return { triggers: newTriggers };
    });
  },

  clearLogs() {
    set({ logs: [] });
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

// Age of a freshness timestamp relative to now, unit-scaled. F=0 → '—'. The numeric part is always
// two digits (e.g. "02s", "47m") so the width never jitters as a value crosses 9↔10; each unit rolls
// to the next before it would exceed 99. Caps at ">1y" — finer resolution past a year isn't useful.
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

// The freshest pending push target (for the ≤ box / edge colour), or null if none are outstanding.
// NEVER (-Inf) targets from `start` are non-finite and excluded — they carry no displayable age.
export function pushTargetF(targets: number[]): number | null {
  const finite = targets.filter((t) => Number.isFinite(t));
  return finite.length ? Math.max(...finite) : null;
}

export function getRippleVisualState(rs: RippleRunState): 'running' | 'queued' | 'idle' {
  if (rs.isRunning) return 'running';
  if (rs.hasPull || rs.targets.length > 0) return 'queued';
  return 'idle';
}

export function getPondVisualState(ps: PondRunState): 'running' | 'queued' | 'idle' {
  if (ps.runsStarted > ps.runsCompleted) return 'running';
  if (ps.hasPull || ps.hasReceivedPull || ps.targets.length > 0) return 'queued';
  return 'idle';
}

export function pondIsIdle(ps: PondRunState | undefined): boolean {
  if (!ps) return true;
  return ps.runsStarted <= ps.runsCompleted && !ps.hasPull && !ps.hasReceivedPull && ps.targets.length === 0;
}

export function rippleIsIdle(rs: RippleRunState | undefined): boolean {
  if (!rs) return true;
  return !rs.isRunning && !rs.hasPull && rs.targets.length === 0;
}

// Edge colour reflects what the SINK is currently demanding of this source:
//   blue  — the sink holds a push target (this source is feeding a push)
//   green — the sink holds pull demand (this source is feeding a resupply)
//   grey  — the sink has no demand on this edge
// `sinkPush` is the sink's push target (or null); `sinkPull` whether it holds a pull token.
export function getDemandEdgeColor(sinkPull: boolean, sinkPush: number | null): string {
  if (sinkPush !== null) return EDGE_COLORS.push;
  if (sinkPull) return EDGE_COLORS.pull;
  return EDGE_COLORS.idle;
}

// Edge colour = most-recent demand kind, but cleared to grey once both endpoints are idle.
export function getEdgeColor(kind: EdgeDemandKind | undefined, sourceIdle: boolean, sinkIdle: boolean): string {
  if (!kind || (sourceIdle && sinkIdle)) return EDGE_COLORS.idle;
  return EDGE_COLORS[kind];
}

export const STATE_COLORS: Record<string, string> = {
  running: '#22c55e',
  queued: '#f97316',
  idle: '#71717a',
};

// pull (Tap/Wave) keeps green; push (Pulse/Tide) blue; stop red.
export const EDGE_COLORS: Record<string, string> = {
  pull: '#22c55e',
  push: '#3b82f6',
  stop: '#ef4444',
  idle: '#3f3f46',
};
