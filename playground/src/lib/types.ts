export type PondId = string;
export type RippleId = string;

export type FreqUnit = 'SECOND' | 'MINUTE' | 'HOUR' | 'DAY' | 'WEEK';
export type Weekday = 'MON' | 'TUE' | 'WED' | 'THU' | 'FRI' | 'SAT' | 'SUN';

// A recurring batch-availability window on an Inlet Pond (no Sources), mirroring the backend's
// RFC-5545-flavoured Window (engine/core.py). The first occurrence opens at `startAnchor` and stays
// "fresh until" startAnchor + durationMs; it then recurs every `freqInterval × freqUnit`. `validDays`
// restricts which weekdays are kept (undefined = every day); `until` ends the recurrence.
// Occurrences are the grid `startAnchor + k·delta` (k ≥ 0), filtered by validDays/until.
export interface Window {
  name: string;
  startAnchor: number; // ms epoch: first occurrence start
  durationMs: number; // window length ("fresh until" startAnchor + durationMs)
  freqUnit: FreqUnit;
  freqInterval: number;
  validDays?: Weekday[]; // undefined / empty = every day
  until?: number; // ms epoch: end of recurrence (undefined = forever)
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
  targets: number[]; // set of unsatisfied push target freshnesses (empty = none)
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
  pullLocal: boolean; // the pull is a Wake: one-shot, does NOT solicit Sources on run start
  targets: number[]; // set of unsatisfied push target freshnesses (empty = none)
  isKilled: boolean; // operator Kill: terminal until a Wake/Force
  isBlocked: boolean; // derived: this Pond or a required Source is killed/blocked
  // Trace data for the sidebar charts:
  runsStarted: number;
  runsCompleted: number;
  startTimesByF: Record<number, number>; // wall-clock start of each in-flight Pond Run, by its startF
  completionTimes: number[];
  durations: number[];
  ages?: number[]; // data age (now - endF) at each completion
  // Pulse release bookkeeping (only used while the release mode is on).
  release?: ReleaseState;
}

// Pulse release, per Pond (see orchestration.ts "Pulse release"). A witness is one of the Pond's own
// Ripples (`r:<id>`) or one of its Source Ponds (`p:<id>`); a witness's runs are told apart by its
// `runsStarted`, so every counter is keyed to "the witness's current run" without timestamps.
export interface ReleaseState {
  counts: Record<string, { run: number; n: number }>; // demands that reached this Pond during each witness's current run
  saturated: string[]; // witnesses proven slower than the demand (two demands arrived during one of their runs)
  forwardedIn: Record<string, number>; // each witness's run number when this Pond last forwarded demand upstream
  forwardedAt: number; // when this Pond last forwarded demand upstream (a Source turns over by starting a later Pond Run)
  held: number; // demands held back (kept as this Pond's own targets, not yet forwarded)
}

// Persistent triggers only (Tap and Pulse are one-shot, no entity).
export type TriggerKind = 'wave' | 'tide';

export interface ActiveTrigger {
  pondId: PondId;
  kind: TriggerKind;
  // Tide only: maximum staleness in ms. A push to `now` fires whenever staleness exceeds this.
  stalenessMs?: number;
}

export type RippleVisualState = 'running' | 'queued' | 'idle';
// Killed/blocked take precedence over demand state, matching the Catchment's status string.
export type PondVisualState = 'killed' | 'blocked' | 'running' | 'queued' | 'idle';

// A single logged orchestration event, for the console panel.
export interface LogEntry {
  t: number; // wall-clock ms of the event
  kind: string; // short category, e.g. 'tap', 'pond-start', 'ripple-done', 'hold', 'release'
  msg: string; // human-readable description
}
