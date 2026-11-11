'use client';

import { useEffect, useRef, useState } from 'react';
import { fetchAppInstances, fetchAppLog, UnauthorizedError } from '@/lib/api';
import { useLiveStore, atLeast, formatAge, parseTs, THEME_BRAND, THEME_DANGER } from '@/lib/store';
import type { AppInstance, PondId } from '@/lib/types';
import { clock } from './RunHistory';

// The bottom panel for an App, in place of Run History and Run Detail (an App's Runs are passes, with nothing to
// show): its instances, newest first, and the selected one's log, which follows a running instance.

const LOG_POLL_MS = 1000;
const LIST_POLL_MS = 2000;
const MAX_LOG_CHARS = 1_000_000; // keep the tail of a long-followed log, not all of it

const mono: React.CSSProperties = { fontFamily: 'ui-monospace, SFMono-Regular, monospace' };

function Bar({ title, detail, children }: { title: string; detail?: string; children?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 30, flexShrink: 0, boxSizing: 'border-box', padding: '0 10px', borderBottom: '1px solid #18181d' }}>
      <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {title}
        {detail && <span style={{ color: '#52525b', fontWeight: 400, marginLeft: 8 }}>{detail}</span>}
      </span>
      {children}
    </div>
  );
}

function stopColor(i: AppInstance): string {
  if (!i.stoppedAt) return i.readyAt ? THEME_BRAND : '#a1a1aa';
  return crashed(i) ? THEME_DANGER : '#71717a';
}

// The exit code only says something when the process ended on its own (a stop sends SIGTERM: -15).
function crashed(i: AppInstance): boolean {
  return i.stopReason === 'exited' || i.stopReason === 'start_failed';
}

function stopLabel(i: AppInstance): string {
  if (!i.stoppedAt) return i.readyAt ? 'running' : 'starting';
  return (i.stopReason ?? 'stopped').replace('_', ' ');
}

function localDay(iso: string): string {
  const d = new Date(parseTs(iso));
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function AppInstances({ pondId, vertical = false }: { pondId: PondId; vertical?: boolean }) {
  const now = useLiveStore((s) => s.now);
  const canReadLogs = useLiveStore((s) => atLeast(s.accessLevel, 'full'));
  // The newest instance id on /api/status: a start or a restart refetches the list at once.
  const liveId = useLiveStore((s) => s.pondInfo[pondId]?.app?.instance?.id ?? null);
  const [instances, setInstances] = useState<AppInstance[]>([]);
  const [selected, setSelected] = useState<number | null>(null); // null = the newest
  const [follow, setFollow] = useState(true);
  const [log, setLog] = useState<{ instance: number; text: string; offset: number; running: boolean } | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  // A different App starts afresh.
  const [prevPond, setPrevPond] = useState(pondId);
  if (pondId !== prevPond) {
    setPrevPond(pondId);
    setInstances([]);
    setSelected(null);
    setLog(null);
    setLogError(null);
  }

  useEffect(() => {
    let alive = true;
    const load = () => fetchAppInstances(pondId)
      .then((rows) => {
        if (!alive) return;
        setInstances(rows.map((r) => ({
          id: r.id, version: r.version, startedAt: r.started_at, readyAt: r.ready_at, stoppedAt: r.stopped_at,
          stopReason: r.stop_reason, exitCode: r.exit_code,
        })));
      })
      .catch((e) => { if (e instanceof UnauthorizedError) useLiveStore.setState({ needsKey: true }); });
    void load();
    const timer = setInterval(load, LIST_POLL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [pondId, liveId]);

  const target = selected ?? instances[0]?.id ?? null;

  // Load the tail of the selected instance's log, then follow it while it runs.
  useEffect(() => {
    if (!canReadLogs || target === null) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let offset: number | null = null;
    const step = async () => {
      try {
        const chunk = await fetchAppLog(pondId, offset === null ? { instance: target } : { instance: target, offset });
        if (!alive) return;
        const first = offset === null;
        offset = chunk.offset;
        setLogError(null);
        setLog((prev) => {
          const text = first || !prev || prev.instance !== target ? chunk.text : prev.text + chunk.text;
          return { instance: target, text: text.length > MAX_LOG_CHARS ? text.slice(-MAX_LOG_CHARS) : text,
                   offset: chunk.offset, running: chunk.running };
        });
        if (chunk.running && follow) timer = setTimeout(step, LOG_POLL_MS);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) useLiveStore.setState({ needsKey: true });
        else setLogError(e instanceof Error ? e.message : String(e));
      }
    };
    void step();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [pondId, target, follow, canReadLogs]);

  // Keep a followed log scrolled to its end.
  useEffect(() => {
    const el = logRef.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [log?.text, follow]);

  const toggle = (
    <button
      onClick={() => setFollow((v) => !v)}
      style={{
        background: follow ? `${THEME_BRAND}20` : 'transparent', border: `1px solid ${follow ? THEME_BRAND : '#3f3f46'}`,
        color: follow ? THEME_BRAND : '#71717a', borderRadius: 4, padding: '2px 8px', fontSize: 10, fontWeight: 600,
        cursor: 'pointer', letterSpacing: '0.04em', fontFamily: 'inherit',
      }}
    >
      follow
    </button>
  );

  let prevDay = '';
  const list = (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: '#0c0c10', minWidth: 0, minHeight: 0, ...mono }}>
      <Bar title="INSTANCES" detail={`${instances.length} · ${pondId}`} />
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 10px 8px', fontSize: 11, lineHeight: 1.6 }}>
        {instances.length === 0 ? (
          <div style={{ color: '#52525b' }}>No instances yet. The App starts on its first request.</div>
        ) : instances.map((i) => {
          const day = localDay(i.startedAt);
          const divider = day !== prevDay;
          prevDay = day;
          const isSel = target === i.id;
          const end = i.stoppedAt ? parseTs(i.stoppedAt) : now;
          return (
            <div key={i.id}>
              {divider && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '8px 0 2px' }}>
                  <span style={{ color: '#71717a', fontSize: 10, fontWeight: 700, letterSpacing: '0.08em' }}>{day}</span>
                  <span style={{ flex: 1, height: 1, background: '#27272a' }} />
                </div>
              )}
              <div
                onClick={() => setSelected(i.id === instances[0]?.id ? null : i.id)}
                style={{
                  display: 'flex', gap: 8, whiteSpace: 'pre', cursor: 'pointer', borderRadius: 4, padding: '0 4px', margin: '0 -4px',
                  background: isSel ? `${THEME_BRAND}1f` : 'transparent', boxShadow: isSel ? `inset 2px 0 0 ${THEME_BRAND}` : undefined,
                }}
              >
                <span style={{ color: '#3f3f46' }}>{clock(i.startedAt)}</span>
                <span style={{ width: 80, overflow: 'hidden', textOverflow: 'ellipsis', color: stopColor(i) }}>{stopLabel(i)}</span>
                <span style={{ width: 52, overflow: 'hidden', textOverflow: 'ellipsis', color: '#52525b' }}>v{i.version}</span>
                <span style={{ color: '#71717a' }}>{formatAge(parseTs(i.startedAt), end)}</span>
                {crashed(i) && i.exitCode !== null && <span style={{ color: '#52525b' }}>exit {i.exitCode}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );

  const logPane = (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: '#0c0c10', minWidth: 0, minHeight: 0, ...mono }}>
      <Bar title="LOG" detail={target !== null ? `instance ${target}${log?.running ? ' · running' : ''}` : undefined}>
        {canReadLogs && target !== null && toggle}
      </Bar>
      {!canReadLogs ? (
        <div style={{ padding: '8px 10px', fontSize: 11, color: '#52525b' }}>Logs need full access.</div>
      ) : logError ? (
        <div style={{ padding: '8px 10px', fontSize: 11, color: THEME_DANGER }}>{logError}</div>
      ) : (
        <pre
          ref={logRef}
          style={{
            flex: 1, margin: 0, padding: '6px 10px', overflow: 'auto', fontSize: 11, lineHeight: 1.5, color: '#a1a1aa',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word', ...mono,
          }}
        >
          {target === null ? '' : log?.instance === target ? log.text || '(no output yet)' : ''}
        </pre>
      )}
    </div>
  );

  if (vertical) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        <div style={{ flex: 1, minHeight: 0 }}>{list}</div>
        <div style={{ flex: 1, minHeight: 0, borderTop: '1px solid #27272a' }}>{logPane}</div>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0 }}>
      <div style={{ flex: 1, minWidth: 0, borderRight: '1px solid #27272a' }}>{list}</div>
      <div style={{ flex: 2, minWidth: 0 }}>{logPane}</div>
    </div>
  );
}
