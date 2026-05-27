export type PondId = string;
export type RippleId = string;
export type SinkId = string | null;

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

export interface DemandRecord {
  sinkId: SinkId;
  isStop: boolean;
  isPersistent: boolean;
}

export interface RippleRunState {
  generation: number;
  isRunning: boolean;
  runStartedAt: number | null;
  demand: DemandRecord[];
}

// key: `${sourceRippleId}::${sinkRippleId}`
export type WatermarkMap = Record<string, number>;

export type TriggerKind = 'wave' | 'tide';

export interface ActiveTrigger {
  pondId: PondId;
  kind: TriggerKind;
  periodMs?: number;
}

export type RippleVisualState = 'running' | 'stopped' | 'queued' | 'idle';
export type DemandVisualState = 'wave' | 'pulse' | 'stop' | 'none';
