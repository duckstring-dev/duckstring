'use client';

import { Fragment, useState } from 'react';
import { useLiveStore, parseTs } from '@/lib/store';
import type { RippleRun } from '@/lib/types';

const STATUS_COLOR: Record<string, string> = {
  success: '#22c55e',
  running: '#f59e0b',
  failed: '#ef4444',
};

// Fixed column widths (px) so the duration lands at the same x on Pond and (indented) Ripple rows.
// Both rows share clock + status; a Pond row then has name + version, a Ripple row a single wider
// name + its indent. Aligning the duration requires: RIPPLE_NAME = NAME + VERSION + gap − INDENT.
const GAP = 8;
const STATUS_W = 64;
const NAME_W = 104;
const VERSION_W = 48;
const RIPPLE_INDENT = 24;
const RIPPLE_NAME_W = NAME_W + VERSION_W + GAP - RIPPLE_INDENT;

const col = (w: number): React.CSSProperties => ({
  width: w,
  display: 'inline-block',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
});

function clock(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(parseTs(iso));
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const WKD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function localYMD(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// Local-day bucket for a Pond Run, keyed on its start. Runs are grouped under this; a Ripple that
// finishes after midnight stays in its Pond Run's (earlier) bucket because dividers are emitted only
// at the Pond-Run level.
function dayKey(iso: string | null): string {
  return iso ? localYMD(new Date(parseTs(iso))) : '';
}

function dayLabel(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(parseTs(iso));
  return `${WKD[d.getDay()]} ${localYMD(d)}`; // "Mon 2026-06-09"
}

function DateDivider({ label }: { label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '8px 0 2px' }}>
      <span style={{ color: '#71717a', fontSize: 10, fontWeight: 700, letterSpacing: '0.08em' }}>{label}</span>
      <span style={{ flex: 1, height: 1, background: '#27272a' }} />
    </div>
  );
}

function durationOf(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt || !finishedAt) return '';
  return `${((parseTs(finishedAt) - parseTs(startedAt)) / 1000).toFixed(1)}s`;
}

function Toggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      style={{
        background: on ? '#6366f120' : 'transparent',
        border: `1px solid ${on ? '#6366f1' : '#3f3f46'}`,
        color: on ? '#a5b4fc' : '#71717a',
        borderRadius: 4,
        padding: '2px 8px',
        fontSize: 10,
        fontWeight: 600,
        cursor: 'pointer',
        letterSpacing: '0.04em',
      }}
    >
      {children}
    </button>
  );
}

function RippleRow({ r }: { r: RippleRun }) {
  return (
    <div style={{ display: 'flex', gap: GAP, whiteSpace: 'pre', paddingLeft: RIPPLE_INDENT }}>
      <span style={{ color: '#3f3f46' }}>{clock(r.finishedAt)}</span>
      <span style={{ ...col(STATUS_W), color: STATUS_COLOR[r.status] ?? '#71717a' }}>{r.status}</span>
      <span style={{ ...col(RIPPLE_NAME_W), color: '#a1a1aa' }}>{r.ripple}</span>
      <span style={{ color: '#71717a' }}>{durationOf(r.startedAt, r.finishedAt)}</span>
    </div>
  );
}

export function RunHistory() {
  const runs = useLiveStore((s) => s.runs);
  const filters = useLiveStore((s) => s.runFilters);
  const setRunFilter = useLiveStore((s) => s.setRunFilter);
  const loadMoreRuns = useLiveStore((s) => s.loadMoreRuns);
  const runsAtEnd = useLiveStore((s) => s.runsAtEnd);
  const selectedPondId = useLiveStore((s) => s.selectedPondId);
  const selectedRippleId = useLiveStore((s) => s.selectedRippleId);
  const ripples = useLiveStore((s) => s.ripples);
  const [open, setOpen] = useState(true);

  const focus = selectedPondId ?? (selectedRippleId ? ripples[selectedRippleId]?.pondId : null) ?? null;

  // Grow the live window when scrolled near the bottom (store guards against over-fetching).
  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (!runsAtEnd && el.scrollHeight - el.scrollTop - el.clientHeight < 120) loadMoreRuns();
  };

  return (
    <div style={{ borderTop: '1px solid #27272a', background: '#0c0c10', fontFamily: 'ui-monospace, SFMono-Regular, monospace', flexShrink: 0 }}>
      <div
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 10px', cursor: 'pointer', userSelect: 'none' }}
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
          {open ? '▾' : '▸'} RUN HISTORY
          <span style={{ color: '#52525b', fontWeight: 400, marginLeft: 8 }}>
            {runsAtEnd ? '' : '>'}{runs.length} run{runs.length === 1 ? '' : 's'}
            {focus ? ` · ${focus}${filters.lineage ? ' + lineage' : ''}` : ' · all'}
          </span>
        </span>
        <span style={{ display: 'inline-flex', gap: 6 }}>
          <Toggle on={filters.lineage} onClick={() => setRunFilter('lineage', !filters.lineage)}>lineage</Toggle>
          <Toggle on={filters.ripples} onClick={() => setRunFilter('ripples', !filters.ripples)}>ripples</Toggle>
        </span>
      </div>

      {open && (
        <div onScroll={onScroll} style={{ height: 200, overflowY: 'auto', padding: '4px 10px 8px', fontSize: 11, lineHeight: 1.6 }}>
          {runs.length === 0 ? (
            <div style={{ color: '#52525b' }}>No runs yet — send a Tap, Pulse, Wave, or Start.</div>
          ) : (
            (() => {
              let prevDay = '';
              return runs.map((run) => {
                const day = dayKey(run.startedAt);
                const showDivider = day !== prevDay;
                prevDay = day;
                return (
                  <Fragment key={`${run.pond}-${run.version}-${run.f}`}>
                    {showDivider && <DateDivider label={dayLabel(run.startedAt)} />}
                    <div style={{ display: 'flex', gap: GAP, whiteSpace: 'pre' }}>
                      <span style={{ color: '#3f3f46' }}>{clock(run.finishedAt ?? run.startedAt)}</span>
                      <span style={{ ...col(STATUS_W), color: STATUS_COLOR[run.status] ?? '#71717a' }}>{run.status}</span>
                      <span style={{ ...col(NAME_W), color: '#d4d4d8' }}>{run.pond}</span>
                      <span style={{ ...col(VERSION_W), color: '#52525b' }}>v{run.version}</span>
                      <span style={{ color: '#71717a' }}>{durationOf(run.startedAt, run.finishedAt)}</span>
                    </div>
                    {filters.ripples && run.ripples?.map((r) => <RippleRow key={r.ripple} r={r} />)}
                  </Fragment>
                );
              });
            })()
          )}
          {runs.length > 0 && (
            <div style={{ color: '#3f3f46', fontSize: 10, textAlign: 'center', padding: '8px 0 2px' }}>
              {runsAtEnd ? '— end of history —' : 'scroll for more…'}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
