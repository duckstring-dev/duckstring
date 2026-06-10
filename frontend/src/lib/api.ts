// Thin typed client for the Catchment HTTP API. In dev, next.config rewrites /api/* to the FastAPI
// server (DUCKSTRING_CATCHMENT_URL / :8000); in the static-export build FastAPI serves this app at
// the same origin, so a relative /api base works in both.

import type { FreqUnit } from './types';

const API = '/api';

// ─── Raw payload shapes (snake_case, as the backend emits) ───────────────────

export interface RawRipple {
  name: string;
  status: 'running' | 'queued' | 'idle';
  gen: number;
  runs_completed: number;
  has_pull: boolean;
  target_f: string | null;
  start_f: string | null;
  end_f: string | null;
}

export interface RawPond {
  name: string;
  kind: string;
  version: string;
  status: 'running' | 'queued' | 'idle' | 'failed' | 'killed' | 'blocked';
  gen: number;
  runs_completed: number;
  has_pull: boolean;
  target_f: string | null;
  start_f: string | null;
  end_f: string | null;
  d_ms: number;
  trigger: { kind: 'wave' | 'tide'; bound_ms: number | null } | null;
  is_failed: boolean;
  is_blocked: boolean;
  is_killed: boolean;
  failed_f: string | null;
  failures: number;
  immediate_retries: number;
  source_retries: number;
  ripples: RawRipple[];
  ripple_edges: [string, string][]; // [sourceName, sinkName] within the Pond
}

export interface StatusPayload {
  ponds: RawPond[];
  edges: [string, string][]; // [sourcePond, sinkPond]
}

export interface RawRippleRun {
  ripple: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  retry: number;
  error: string | null;
  traceback: string | null;
}

export interface RawPondRun {
  pond: string;
  version: string;
  f: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  error: string | null;
  traceback: string | null;
  ripples?: RawRippleRun[];
}

export interface RawWindow {
  name: string;
  start_anchor: string;
  duration_seconds: number;
  freq_unit: FreqUnit;
  freq_interval: number;
  valid_days: string | null;
  until_time: string | null;
}

// ─── Requests ────────────────────────────────────────────────────────────────

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

async function postJSON(path: string, body: unknown = {}): Promise<void> {
  const res = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json())?.detail ?? '';
    } catch {
      /* no body */
    }
    throw new Error(detail || `POST ${path} → ${res.status}`);
  }
}

export function fetchStatus(): Promise<StatusPayload> {
  return getJSON<StatusPayload>('/status');
}

export interface RunsQuery {
  pond?: string | null;
  lineage?: boolean;
  ripples?: boolean;
  limit?: number;
}

export async function fetchRuns(q: RunsQuery = {}): Promise<RawPondRun[]> {
  const params = new URLSearchParams();
  if (q.pond) params.set('pond', q.pond);
  if (q.lineage !== undefined) params.set('lineage', String(q.lineage));
  if (q.ripples !== undefined) params.set('ripples', String(q.ripples));
  if (q.limit !== undefined) params.set('limit', String(q.limit));
  const qs = params.toString();
  const data = await getJSON<{ runs: RawPondRun[] }>(`/runs${qs ? `?${qs}` : ''}`);
  return data.runs;
}

// Trigger / demand actions (the CLI `trigger` surface). `endpoint` is the route segment under
// /api/ponds/{pond}/ — tap | pulse | wave | tide | start | stop | untrigger.
export function postTrigger(pond: string, endpoint: string, body: unknown = {}): Promise<void> {
  return postJSON(`/ponds/${encodeURIComponent(pond)}/${endpoint}`, body);
}

// Failure management.
export function clearFailure(pond: string): Promise<void> {
  return postJSON(`/ponds/${encodeURIComponent(pond)}/clear`);
}

export function setBudget(pond: string, immediateRetries: number, sourceRetries: number): Promise<void> {
  return postJSON(`/ponds/${encodeURIComponent(pond)}/budget`, {
    immediate_retries: immediateRetries,
    source_retries: sourceRetries,
  });
}

export function fetchWindows(pond: string): Promise<RawWindow[]> {
  return getJSON<{ windows: RawWindow[] }>(`/ponds/${encodeURIComponent(pond)}/windows`).then((d) => d.windows);
}

export interface AddWindowBody {
  name: string;
  start_anchor: string;
  duration_seconds: number;
  freq_unit: FreqUnit;
  freq_interval: number;
  valid_days: string | null;
  until_time: string | null;
}

export function addWindow(pond: string, body: AddWindowBody): Promise<void> {
  return postJSON(`/ponds/${encodeURIComponent(pond)}/windows`, body);
}

export function removeWindow(pond: string, name: string): Promise<void> {
  return postJSON(`/ponds/${encodeURIComponent(pond)}/windows/${encodeURIComponent(name)}/remove`);
}
