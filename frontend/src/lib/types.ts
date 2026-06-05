export type PondId = string;
export type RippleId = string;

// A repeating window within each minute, in seconds [startSec, endSec). Only meaningful on an
// Inlet Pond (no Sources): it models a batch source that becomes available at startSec and is
// "fresh until" endSec. Non-overlapping; gaps allowed.
export interface Window {
  startSec: number;
  endSec: number;
}

export interface Pond {
  id: PondId;
  name: string;
  sources: PondId[];
  // Source ponds that are optional (don't gate / don't define freshness). Default: all required.
  optionalSources?: PondId[];
  // Batch-update windows (Inlet only). Empty/undefined ⇒ live source (fresh = now).
  windows?: Window[];
}

export interface Ripple {
  id: RippleId;
  pondId: PondId;
  name: string;
  parents: RippleId[];
  // Intra-pond parents that are optional. Default: all required.
  optionalParents?: RippleId[];
  durationMs: number;
  // Standard deviation applied on a log transform of durationMs: a run takes
  // durationMs * exp(variability * Z), Z ~ N(0,1). 0 = deterministic.
  variability: number;
}

// First-class Ripple state, named exactly as in theory.md.
export interface RippleRunState {
  startF: number; // freshness of the most recently started run (0 = never)
  endF: number; // freshness of the most recently completed run (0 = never)
  hasPull: boolean; // pull token
  targetF: number | null; // push target freshness, or null
  // Run bookkeeping (the simulation of an actual run taking time):
  isRunning: boolean;
  runStartedAt: number | null;
  currentRunDurationMs: number | null;
  lastDurationMs: number | null;
  // Trace data for the sidebar charts:
  runsStarted: number;
  runsCompleted: number;
  completionTimes: number[];
  durations: number[];
}

// First-class Pond state, named exactly as in theory.md.
export interface PondRunState {
  startF: number; // freshness of the most recently started Pond Run
  endF: number; // freshness of the most recently completed Pond Run
  D: number; // window delay carried by the current freshness (0 unless fed by a window)
  hasReceivedPull: boolean; // inbox: a Sink/trigger has asked for resupply
  hasPull: boolean; // a Pond Run is wanted in pull
  targetF: number | null; // push target freshness, or null
  // Trace data for the sidebar charts:
  runsStarted: number;
  runsCompleted: number;
  genStartTimes: Record<number, number>;
  completionTimes: number[];
  durations: number[];
}

// Kind of demand most recently sent across an edge. Keyed `${parent}::${child}` /
// `${sourcePond}::${sinkPond}`.
export type EdgeDemandKind = 'push' | 'pull' | 'stop';
export type EdgeKindMap = Record<string, EdgeDemandKind>;

// Persistent triggers only (Tap and Pulse are one-shot, no entity).
export type TriggerKind = 'wave' | 'tide';

export interface ActiveTrigger {
  pondId: PondId;
  kind: TriggerKind;
  // Tide only: maximum staleness in ms. A push to `now` fires whenever staleness exceeds this.
  stalenessMs?: number;
}

export type RippleVisualState = 'running' | 'queued' | 'idle';
export type PondVisualState = 'running' | 'queued' | 'idle';
export type EdgeVisualState = 'push' | 'pull' | 'stop' | 'idle';

// A single logged orchestration event, for the console panel.
export interface LogEntry {
  t: number; // wall-clock ms of the event
  kind: string; // short category, e.g. 'tap', 'pond-start', 'ripple-done'
  msg: string; // human-readable description
}
