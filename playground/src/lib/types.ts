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
  major?: number; // major version (default 1); each major line is its own Pond with the same name
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

// A Ripple's run state.
export interface RippleRunState {
  startF: number; // freshness of the most recently started run (0 = never)
  endF: number; // freshness of the most recently completed run (0 = never)
  targets: number[]; // set of unsatisfied requested freshnesses (empty = none)
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

// A Pond's run state.
export interface PondRunState {
  startF: number; // freshness of the most recently started Pond Run
  endF: number; // freshness of the most recently completed Pond Run
  D: number; // window delay carried by the current freshness (0 unless fed by a window)
  targets: number[]; // set of unsatisfied requested freshnesses: the Pond's demand (empty = none)
  isKilled: boolean; // Kill: terminal until a Force
  isBlocked: boolean; // derived: this Pond or a required Source is killed/blocked
  // Trace data for the sidebar charts:
  runsStarted: number;
  runsCompleted: number;
  startTimesByF: Record<number, number>; // wall-clock start of each in-flight Pond Run, by its startF
  completionTimes: number[];
  durations: number[];
  ages?: number[]; // data age (now - endF) at each completion
  // Throttle bookkeeping.
  throttle?: ThrottleState;
}

// The throttle, per Pond (see orchestration.ts "Throttle").
export interface ThrottleState {
  counts: Record<string, { run: number; n: number }>; // demands that reached this Pond during each of its Ripples' current runs
  slow: string[]; // own Ripples that saw two demands in one run, until idle with no demand waiting
  held: number; // demands held at this Pond since it last sent a request upstream
  trigger?: boolean; // a Refresh targets this Pond: when throttled it paces on its completions
  forwardedIn: Record<string, number>; // each Ripple's runsStarted when this Pond last sent a request upstream
  deferred: Record<string, number>; // sibling alignment: newest target held back from each unthrottled Source
  deferredAt: Record<string, number>; // the throttled Sources' runsStarted when targets were first held back
  // Launch alignment (EXPERIMENT): each Ripple's recent leads (from the freshness of an input to when it was ready),
  // the input freshness last seen, and when a bottleneck's due release goes out.
  leads?: Record<string, number[]>;
  seenF?: Record<string, number>;
  launchAt?: number | null;
}

// A recurring Refresh on a Pond. Floating (no `startAnchor`): a request once `everyMs` has passed since the later of
// the Pond's newest request and its freshness (0 = continuous). Anchored: a request at each occurrence of the grid
// `startAnchor + k·everyMs`, filtered by `validDays` and ended by `until`, as a window's grid is. A Pond holds any
// number of anchored Refreshes and at most one floating one.
export interface Refresh {
  name: string;
  everyMs: number;
  startAnchor?: number; // ms epoch; set = anchored
  validDays?: Weekday[]; // undefined / empty = every day
  until?: number; // ms epoch: end of recurrence (undefined = forever)
}

export type RippleVisualState = 'running' | 'queued' | 'idle';
// Killed/blocked take precedence over demand state, matching the Catchment's status string.
export type PondVisualState = 'killed' | 'blocked' | 'running' | 'queued' | 'idle';

// A single logged orchestration event, for the console panel.
export interface LogEntry {
  t: number; // wall-clock ms of the event
  kind: string; // short category, e.g. 'refresh', 'pond-start', 'ripple-done', 'queue', 'release'
  msg: string; // human-readable description
}
