export type PondId = string;
export type RippleId = string;

export interface Pond {
  id: PondId;
  name: string;
  sources: PondId[];
  // Source ponds that are optional (don't gate / don't define freshness). Default: all required.
  optionalSources?: PondId[];
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

// Freshness-based run state. `F` is output freshness (a timestamp); demand is `hasPull`
// (resupply, Tap/Wave) and `hasPush` (priority target T, Pulse/Tide). See orchestration.ts.
export interface RippleRunState {
  F: number; // output freshness timestamp (0 = never run)
  hasPull: boolean; // resupply demand (Tap/Wave)
  hasPush: number | null; // priority freshness target T (Pulse/Tide), else null
  runFreshness: number | null; // parentsFreshness captured at the in-flight run's start
  isRunning: boolean;
  runStartedAt: number | null;
  currentRunDurationMs: number | null; // sampled duration of the in-flight run
  lastDurationMs: number | null; // sampled duration of the most recent completed run
  completionTimes: number[]; // ms timestamps of completions — cadence trace
  durations: number[]; // sampled run durations (ms) — duration trace
  runsStarted: number;
  runsCompleted: number;
}

// Derived-each-tick pond rollup for display.
export interface PondRunState {
  F: number; // completed-run freshness: min over leaf ripples' F
  startedF: number; // started-run freshness: min over leaves of (in-flight runFreshness else F)
  hasPull: boolean; // any leaf pulling
  hasPush: number | null; // max leaf push target
  runsStarted: number; // max over root ripples
  runsCompleted: number; // min over leaf ripples
  // Start timestamp per pond generation number (keyed by runsStarted), so a generation's latency
  // is measured against ITS OWN start, not the latest start (which differs under pipelining).
  genStartTimes: Record<number, number>;
  completionTimes: number[]; // when runsCompleted advanced — cadence trace
  durations: number[]; // generation latency (completion − that generation's start) — duration trace
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
  periodMs?: number;
  // Wave is modelled as a zero-duration pseudo-ripple consuming the pond's leaves: it holds the
  // freshness it has last "consumed", so the leaves are gated together by ONE consumer (throttled
  // to the slowest) rather than each being re-armed independently. Undefined until first consume.
  consumedF?: number;
}

export type RippleVisualState = 'running' | 'queued' | 'idle';
export type PondVisualState = 'running' | 'queued' | 'wave' | 'idle';
export type EdgeVisualState = 'push' | 'pull' | 'stop' | 'idle';
