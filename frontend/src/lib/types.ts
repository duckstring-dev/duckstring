// Live Catchment UI types. The DAG topology mirrors the playground's shape (Ponds keyed by name,
// Ripples keyed by `${pond}.${ripple}`) so the layout engine (lib/layout.ts) is reused unchanged.
// Per-node live state is carried in a NodeView, fed from the enriched /api/status payload.

export type PondId = string; // the Pond name
export type RippleId = string; // `${pond}.${ripple}`

export type DemandStatus = 'running' | 'queued' | 'idle';
export type FreqUnit = 'SECOND' | 'MINUTE' | 'HOUR' | 'DAY' | 'WEEK';
export type Weekday = 'MON' | 'TUE' | 'WED' | 'THU' | 'FRI' | 'SAT' | 'SUN';
export type TriggerKind = 'wave' | 'tide';

// ─── Topology (drives layout) ────────────────────────────────────────────────

export interface Pond {
  id: PondId;
  name: string;
  kind: string; // inlet | pond | outlet
  sources: PondId[];
}

export interface Ripple {
  id: RippleId;
  pondId: PondId;
  name: string;
  parents: RippleId[];
}

// ─── Live per-node state ─────────────────────────────────────────────────────

// Freshness is a ms-epoch (0 = NEVER, so formatAge renders "—"); targetF is null when there is no
// outstanding push target (vs. 0, which would be a real timestamp).
export interface NodeView {
  status: DemandStatus;
  startF: number;
  endF: number;
  targetF: number | null;
  hasPull: boolean;
  runsStarted: number;
  runsCompleted: number;
  dMs: number; // window delay carried by the current freshness (Pond only; 0 for Ripples)
}

export interface TriggerView {
  kind: TriggerKind;
  boundMs: number | null; // Tide only: the staleness bound
}

export interface PondInfo {
  version: string;
  kind: string;
}

// ─── Run history ─────────────────────────────────────────────────────────────

export interface RippleRun {
  ripple: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: string;
}

export interface PondRun {
  pond: string;
  version: string;
  f: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: string;
  ripples?: RippleRun[];
}

// ─── Windows ─────────────────────────────────────────────────────────────────

// Matches the backend list_windows shape (ISO start/until, seconds duration). Operational config
// managed via the API, not declared in pond.toml.
export interface WindowRow {
  name: string;
  startAnchor: string;
  durationSeconds: number;
  freqUnit: FreqUnit;
  freqInterval: number;
  validDays: string | null;
  untilTime: string | null;
}
