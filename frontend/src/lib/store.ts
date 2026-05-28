import { create } from 'zustand';
import type {
  PondId,
  RippleId,
  Pond,
  Ripple,
  PondRunState,
  RippleRunState,
  WatermarkMap,
  ActiveTrigger,
} from './types';
import { hasCyclePond, hasCycleRipple, isPondDownstreamOf } from './graph';
import {
  tick as orchTick,
  receivePulseAtEnd,
  receiveWaveAtEnd,
  receiveStopAtEnd,
  receiveStart,
  type OrchestrState,
} from './orchestration';

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
    generationStarted: 0,
    generationCompleted: 0,
    hasDemand: false,
    isWave: false,
  };
}

function initialRippleState(): RippleRunState {
  return {
    generationStarted: 0,
    generationCompleted: 0,
    isRunning: false,
    runStartedAt: null,
    hasDemand: false,
    isWave: false,
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
      [txIngestId]: { id: txIngestId, pondId: txId, name: 'ingest', parents: [], durationMs: 1000 },
      [prodIngestId]: { id: prodIngestId, pondId: prodId, name: 'ingest', parents: [], durationMs: 2000 },
      [dailySalesId]: { id: dailySalesId, pondId: salesId, name: 'daily_sales', parents: [], durationMs: 2000 },
      [priceTiersId]: { id: priceTiersId, pondId: salesId, name: 'price_tiers', parents: [], durationMs: 1000 },
      [joinLinesId]: { id: joinLinesId, pondId: salesId, name: 'join_lines', parents: [dailySalesId, priceTiersId], durationMs: 3000 },
      [monthlySummaryId]: { id: monthlySummaryId, pondId: reportsId, name: 'monthly_summary', parents: [], durationMs: 1000 },
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
  watermarks: WatermarkMap;
  selectedPondId: PondId | null;
  selectedRippleId: RippleId | null;
  selectedTriggerId: PondId | null;
  triggers: Record<PondId, ActiveTrigger>;
  tideIntervals: Record<PondId, ReturnType<typeof setInterval>>;

  addPond(): void;
  addRipple(pondId: PondId): void;
  setRippleDuration(rippleId: RippleId, ms: number): void;
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

  triggerPulse(pondId: PondId): void;
  triggerWave(pondId: PondId): void;
  triggerTide(pondId: PondId, periodMs: number): void;
  triggerStop(pondId: PondId): void;
  triggerStart(pondId: PondId): void;
  removeTrigger(pondId: PondId): void;

  tick(now: number): void;
}

function toOrchestrState(state: PlaygroundState): OrchestrState {
  return {
    ponds: state.ponds,
    pondStates: state.pondStates,
    ripples: state.ripples,
    rippleStates: state.rippleStates,
    watermarks: state.watermarks,
    triggers: state.triggers,
  };
}

function applyOrch(_prev: PlaygroundState, orch: OrchestrState): Partial<PlaygroundState> {
  return {
    pondStates: orch.pondStates,
    rippleStates: orch.rippleStates,
    watermarks: orch.watermarks,
    triggers: orch.triggers,
  };
}

function isOutlet(ponds: Record<PondId, Pond>, pondId: PondId): boolean {
  return !Object.values(ponds).some((p) => isPondDownstreamOf(ponds, p.id, pondId) && p.id !== pondId);
}

export const usePlaygroundStore = create<PlaygroundState>((set, get) => ({
  ponds: demoState.ponds,
  pondStates: demoState.pondStates,
  ripples: demoState.ripples,
  rippleStates: demoState.rippleStates,
  watermarks: {},
  selectedPondId: null,
  selectedRippleId: null,
  selectedTriggerId: null,
  triggers: {},
  tideIntervals: {},

  addPond() {
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
  },

  addRipple(pondId) {
    const rippleId = newRippleId();
    const ripple: Ripple = {
      id: rippleId,
      pondId,
      name: newRippleName(pondId),
      parents: [],
      durationMs: 1000,
    };
    set((s) => ({
      ripples: { ...s.ripples, [rippleId]: ripple },
      rippleStates: { ...s.rippleStates, [rippleId]: initialRippleState() },
    }));
  },

  setRippleDuration(rippleId, ms) {
    set((s) => ({
      ripples: { ...s.ripples, [rippleId]: { ...s.ripples[rippleId], durationMs: ms } },
    }));
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

  triggerPulse(pondId) {
    set((s) => applyOrch(s, receivePulseAtEnd(pondId, toOrchestrState(s))));
  },

  triggerWave(pondId) {
    const state = get();
    if (!isOutlet(state.ponds, pondId)) return;
    state.removeTrigger(pondId);
    const trigger: ActiveTrigger = { pondId, kind: 'wave' };
    set((s) => {
      const orchWithTrigger: OrchestrState = {
        ...toOrchestrState(s),
        triggers: { ...s.triggers, [pondId]: trigger },
      };
      return applyOrch(s, receiveWaveAtEnd(pondId, orchWithTrigger));
    });
  },

  triggerTide(pondId, periodMs) {
    get().removeTrigger(pondId);
    const trigger: ActiveTrigger = { pondId, kind: 'tide', periodMs };
    const pulse = () => {
      set((s) => applyOrch(s, receivePulseAtEnd(pondId, toOrchestrState(s))));
    };
    const intervalId = setInterval(pulse, periodMs);
    set((s) => ({
      triggers: { ...s.triggers, [pondId]: trigger },
      tideIntervals: { ...s.tideIntervals, [pondId]: intervalId },
    }));
    pulse();
  },

  triggerStop(pondId) {
    get().removeTrigger(pondId);
    set((s) => applyOrch(s, receiveStopAtEnd(pondId, toOrchestrState(s))));
  },

  triggerStart(pondId) {
    set((s) => applyOrch(s, receiveStart(pondId, toOrchestrState(s))));
  },

  removeTrigger(pondId) {
    set((s) => {
      const intervalId = s.tideIntervals[pondId];
      if (intervalId !== undefined) clearInterval(intervalId);
      const newIntervals = { ...s.tideIntervals };
      delete newIntervals[pondId];
      const newTriggers = { ...s.triggers };
      delete newTriggers[pondId];
      return { triggers: newTriggers, tideIntervals: newIntervals };
    });
  },

  tick(now) {
    set((s) => applyOrch(s, orchTick(now, toOrchestrState(s))));
  },
}));

// ─── Visual state helpers ────────────────────────────────────────────────────

export function getRippleVisualState(rs: RippleRunState): 'running' | 'queued' | 'idle' {
  if (rs.isRunning) return 'running';
  if (rs.hasDemand) return 'queued';
  return 'idle';
}

export function getPondVisualState(ps: PondRunState): 'running' | 'queued' | 'wave' | 'idle' {
  if (ps.generationStarted > ps.generationCompleted) return 'running';
  if (ps.hasDemand) return 'queued';
  if (ps.isWave) return 'wave';
  return 'idle';
}

export function getPondEdgeVisualState(sourcePs: PondRunState | undefined): 'wave' | 'pulse' | 'idle' {
  if (!sourcePs) return 'idle';
  if (sourcePs.isWave) return 'wave';
  if (sourcePs.hasDemand) return 'pulse';
  return 'idle';
}

export const STATE_COLORS: Record<string, string> = {
  running: '#22c55e',
  queued: '#f97316',
  wave: '#22c55e',
  idle: '#71717a',
};

export const EDGE_COLORS: Record<string, string> = {
  wave: '#22c55e',
  pulse: '#3b82f6',
  idle: '#3f3f46',
};
