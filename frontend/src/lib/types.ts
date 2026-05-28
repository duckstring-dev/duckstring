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

export interface PondRunState {
  generationStarted: number;
  generationCompleted: number;
  hasDemand: boolean;
  isWave: boolean;
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
