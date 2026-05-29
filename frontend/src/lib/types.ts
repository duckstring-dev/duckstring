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
  hasDemand: boolean;
  // Pending wave intent: set by a wave reaching P.start, cleared by advancePond.
  isWave: boolean;
  generations: Record<number, PondGeneration>;
}

export interface RippleRunState {
  generationStarted: number;
  generationCompleted: number;
  isRunning: boolean;
  runStartedAt: number | null;
  hasDemand: boolean;
}

// Watermark keys:
//   `${parentRippleId}::${childRippleId}` — intra-pond ripple parent → child
//   `${sourcePondId}::${sinkPondId}`      — pond-level (held by sink against source)
export type WatermarkMap = Record<string, number>;

export type TriggerKind = 'wave' | 'tide';

export interface ActiveTrigger {
  pondId: PondId;
  kind: TriggerKind;
  periodMs?: number;
}

export type RippleVisualState = 'running' | 'queued' | 'idle';
export type PondVisualState = 'running' | 'queued' | 'wave' | 'idle';
export type EdgeVisualState = 'wave' | 'pulse' | 'idle';
