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

// Demand, wave mode, and stopped state live at the Pond level.
export interface PondRunState {
  isStopped: boolean;          // true initially; cleared on non-stop demand; set again after run completes if only stop demand remains
  demand: DemandRecord[];      // per-sink demand records
  hasDemand: boolean;          // any non-stop demand waiting — gates tryStartPond
  isWave: boolean;             // any persistent demand — cleared on start, allowing pulse demotion
  generationStarted: number;
  generationCompleted: number; // min of all ripple generationCompleted in this pond
}

// Ripples are simplified: no demand records, just generation tracking.
export interface RippleRunState {
  generationStarted: number;
  generationCompleted: number;
  isRunning: boolean;
  runStartedAt: number | null;
  hasDemand: boolean;          // set by pond on cold-start or leaf wake; drives wave propagation to intra-pond parents on start
  isWave: boolean;             // propagate wave to intra-pond parents on start
}

// key: `${sourcePondId}::${sinkPondId}`
export type WatermarkMap = Record<string, number>;

export type TriggerKind = 'wave' | 'tide';

export interface ActiveTrigger {
  pondId: PondId;
  kind: TriggerKind;
  periodMs?: number;
}

export type RippleVisualState = 'running' | 'stopped' | 'queued' | 'idle';
export type DemandVisualState = 'wave' | 'pulse' | 'stop' | 'none';
