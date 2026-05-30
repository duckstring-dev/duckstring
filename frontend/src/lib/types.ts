export type PondId = string;
export type RippleId = string;

export interface Pond {
  id: PondId;
  name: string;
  sources: PondId[];
}

export interface Ripple {
  id: RippleId;
  pondId: PondId;
  name: string;
  parents: RippleId[];
  durationMs: number;
  // Standard deviation applied on a log transform of durationMs: a run takes
  // durationMs * exp(variability * Z), Z ~ N(0,1). 0 = deterministic.
  variability: number;
}

// One run of a Pond. isWave is captured from pond.isWave when the generation is
// armed (in advancePond) and is the source of truth for whether ripples in this
// run execute as wave — pond.isWave itself clears immediately on advance.
export interface PondGeneration {
  number: number;
  isWave: boolean;
}

export interface PondRunState {
  generationStarted: number;
  generationCompleted: number;
  // Demand reaching P.start via a root ripple (the chain wants to produce).
  hasRootDemand: boolean;
  // Demand received from a downstream sink at P.end (someone wants the output).
  // Both are required to advance the pond; advancePond clears both.
  hasLeafDemand: boolean;
  // Pending wave intent: set by a wave reaching P.start, cleared by advancePond.
  isWave: boolean;
  generations: Record<number, PondGeneration>;
  // Timestamps (ms) at which the pond completed a generation — for the run-cadence trace.
  completionTimes: number[];
}

export interface RippleRunState {
  generationStarted: number;
  generationCompleted: number;
  isRunning: boolean;
  runStartedAt: number | null;
  hasDemand: boolean;
  // Sampled duration of the in-flight run (base * exp(variability*Z)); null when idle.
  currentRunDurationMs: number | null;
  // Sampled duration of the most recently completed run.
  lastDurationMs: number | null;
  // Timestamps (ms) at which this ripple completed a run — for the run-cadence trace.
  completionTimes: number[];
}

// Watermark keys:
//   `${parentRippleId}::${childRippleId}` — intra-pond ripple parent → child
//   `${sourcePondId}::${sinkPondId}`      — pond-level (held by sink against source)
export type WatermarkMap = Record<string, number>;

// Kind of demand most recently sent across an edge (keyed like WatermarkMap).
export type EdgeDemandKind = 'wave' | 'pulse' | 'stop';
export type EdgeKindMap = Record<string, EdgeDemandKind>;

export type TriggerKind = 'wave' | 'tide';

export interface ActiveTrigger {
  pondId: PondId;
  kind: TriggerKind;
  periodMs?: number;
}

export type RippleVisualState = 'running' | 'queued' | 'idle';
export type PondVisualState = 'running' | 'queued' | 'wave' | 'idle';
export type EdgeVisualState = 'wave' | 'pulse' | 'idle';
