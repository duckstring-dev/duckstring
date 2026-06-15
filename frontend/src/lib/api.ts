// Thin typed client for the Catchment HTTP API. In dev, next.config rewrites /api/* to the FastAPI
// server (DUCKSTRING_CATCHMENT_URL / :8000); in the static-export build FastAPI serves this app —
// possibly under a path prefix (a reverse proxy, Posit Connect's /content/{guid}/), so the API base
// is derived from where the page is mounted rather than hard-coded to the origin root.

import type { FreqUnit } from './types';

function apiBase(): string {
  if (typeof window === 'undefined') return '/api';
  const p = window.location.pathname;
  // The app is a single root-level page: its directory is the Catchment mount point.
  const dir = p.endsWith('/') ? p : p.slice(0, p.lastIndexOf('/') + 1);
  return `${dir}api`;
}

// ─── API key (for a Catchment started with --key) ────────────────────────────
// Kept in localStorage; attached as a Bearer header on every request. A 401 raises
// UnauthorizedError so the store can surface the key prompt.

const KEY_STORAGE = 'duckstring.apiKey';

export class UnauthorizedError extends Error {
  constructor() {
    super('The Catchment requires an API key');
    this.name = 'UnauthorizedError';
  }
}

export function getApiKey(): string | null {
  try {
    return window.localStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
}

export function setApiKey(key: string | null): void {
  try {
    if (key) window.localStorage.setItem(KEY_STORAGE, key);
    else window.localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* storage unavailable — the key just won't persist */
  }
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const key = getApiKey();
  return key ? { ...extra, authorization: `Bearer ${key}` } : extra;
}

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
  id: string; // the pond key "name@major" — the runtime identity of one major line
  name: string;
  major: number;
  kind: string;
  is_draw: boolean; // a Pond Draw — fed by a duct from an upstream Catchment, not run by a Duck
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
  missing_sources: string[]; // declared Sources absent from the Catchment (pond keys "name@major")
  blocked_by: string[]; // required Sources that are down (failed/killed/blocked) — the upstream block
  error: string | null; // failure message of the freshest failed Run, when failed
  immediate_retries: number;
  source_retries: number;
  ripples: RawRipple[];
  ripple_edges: [string, string][]; // [sourceName, sinkName] within the Pond
}

export interface StatusPayload {
  catchment: { id: string | null; name: string | null } | null; // this Catchment's stable identity
  ponds: RawPond[];
  edges: [string, string][]; // [sourceId, sinkId] — pond keys ("name@major")
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
  id: string; // pond key "name@major"
  major: number;
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
  const res = await fetch(`${apiBase()}${path}`, { cache: 'no-store', headers: authHeaders() });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

async function postJSON(path: string, body: unknown = {}): Promise<void> {
  const res = await fetch(`${apiBase()}${path}`, {
    method: 'POST',
    headers: authHeaders({ 'content-type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new UnauthorizedError();
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

// A pond id ("name@major") → the route path + query addressing that major line. All pond-targeting
// routes are keyed by name with a `major` query param.
function pondPath(id: string, rest: string): string {
  const at = id.lastIndexOf('@');
  const name = at === -1 ? id : id.slice(0, at);
  const major = at === -1 ? null : id.slice(at + 1);
  const suffix = major === null ? '' : `${rest.includes('?') ? '&' : '?'}major=${major}`;
  return `/ponds/${encodeURIComponent(name)}/${rest}${suffix}`;
}

export interface RunsQuery {
  pond?: string | null; // a pond id ("name@major")
  lineage?: boolean;
  ripples?: boolean;
  limit?: number;
}

export async function fetchRuns(q: RunsQuery = {}): Promise<RawPondRun[]> {
  const params = new URLSearchParams();
  if (q.pond) {
    const at = q.pond.lastIndexOf('@');
    params.set('pond', at === -1 ? q.pond : q.pond.slice(0, at));
    if (at !== -1) params.set('major', q.pond.slice(at + 1));
  }
  if (q.lineage !== undefined) params.set('lineage', String(q.lineage));
  if (q.ripples !== undefined) params.set('ripples', String(q.ripples));
  if (q.limit !== undefined) params.set('limit', String(q.limit));
  const qs = params.toString();
  const data = await getJSON<{ runs: RawPondRun[] }>(`/runs${qs ? `?${qs}` : ''}`);
  return data.runs;
}

// Trigger / demand actions (the CLI `trigger` surface). `pond` is a pond id ("name@major");
// `endpoint` is the route segment under /api/ponds/{name}/ — tap | pulse | wave | tide | wake |
// sleep | force | kill | untrigger.
export function postTrigger(pond: string, endpoint: string, body: unknown = {}): Promise<void> {
  return postJSON(pondPath(pond, endpoint), body);
}

// Failure management.
export function clearFailure(pond: string): Promise<void> {
  return postJSON(pondPath(pond, 'clear'));
}

export function setBudget(pond: string, immediateRetries: number, sourceRetries: number): Promise<void> {
  return postJSON(pondPath(pond, 'budget'), {
    immediate_retries: immediateRetries,
    source_retries: sourceRetries,
  });
}

export function fetchWindows(pond: string): Promise<RawWindow[]> {
  return getJSON<{ windows: RawWindow[] }>(pondPath(pond, 'windows')).then((d) => d.windows);
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
  return postJSON(pondPath(pond, 'windows'), body);
}

export function removeWindow(pond: string, name: string): Promise<void> {
  return postJSON(pondPath(pond, `windows/${encodeURIComponent(name)}/remove`));
}
