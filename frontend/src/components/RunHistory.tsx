'use client';

import { useState } from 'react';
import { useLiveStore } from '@/lib/store';
import type { PondRun, RippleRun } from '@/lib/types';

const STATUS_COLOR: Record<string, string> = {
  success: '#22c55e',
  running: '#f59e0b',
  failed: '#ef4444',
};

function clock(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function duration(run: PondRun): string {
  if (!run.startedAt || !run.finishedAt) return '';
  const s = (Date.parse(run.finishedAt) - Date.parse(run.startedAt)) / 1000;
  return `${s.toFixed(1)}s`;
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
    <div style={{ display: 'flex', gap: 8, whiteSpace: 'pre', paddingLeft: 28 }}>
      <span style={{ color: '#3f3f46' }}>{clock(r.finishedAt)}</span>
      <span style={{ color: STATUS_COLOR[r.status] ?? '#71717a', minWidth: 60, display: 'inline-block' }}>{r.status}</span>
      <span style={{ color: '#a1a1aa' }}>{r.ripple}</span>
    </div>
  );
}

export function RunHistory() {
  const runs = useLiveStore((s) => s.runs);
  const filters = useLiveStore((s) => s.runFilters);
  const setRunFilter = useLiveStore((s) => s.setRunFilter);
  const selectedPondId = useLiveStore((s) => s.selectedPondId);
  const selectedRippleId = useLiveStore((s) => s.selectedRippleId);
  const ripples = useLiveStore((s) => s.ripples);
  const [open, setOpen] = useState(true);

  const focus = selectedPondId ?? (selectedRippleId ? ripples[selectedRippleId]?.pondId : null) ?? null;

  return (
    <div style={{ borderTop: '1px solid #27272a', background: '#0c0c10', fontFamily: 'ui-monospace, SFMono-Regular, monospace', flexShrink: 0 }}>
      <div
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 10px', cursor: 'pointer', userSelect: 'none' }}
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
          {open ? '▾' : '▸'} RUN HISTORY
          <span style={{ color: '#52525b', fontWeight: 400, marginLeft: 8 }}>
            {runs.length} run{runs.length === 1 ? '' : 's'}
            {focus ? ` · ${focus}${filters.lineage ? ' + lineage' : ''}` : ' · all'}
          </span>
        </span>
        <span style={{ display: 'inline-flex', gap: 6 }}>
          <Toggle on={filters.lineage} onClick={() => setRunFilter('lineage', !filters.lineage)}>lineage</Toggle>
          <Toggle on={filters.ripples} onClick={() => setRunFilter('ripples', !filters.ripples)}>ripples</Toggle>
        </span>
      </div>

      {open && (
        <div style={{ height: 200, overflowY: 'auto', padding: '4px 10px 8px', fontSize: 11, lineHeight: 1.6 }}>
          {runs.length === 0 ? (
            <div style={{ color: '#52525b' }}>No runs yet — send a Tap, Pulse, Wave, or Start.</div>
          ) : (
            runs.map((run) => (
              <div key={`${run.pond}-${run.version}-${run.f}`}>
                <div style={{ display: 'flex', gap: 8, whiteSpace: 'pre' }}>
                  <span style={{ color: '#3f3f46' }}>{clock(run.finishedAt ?? run.startedAt)}</span>
                  <span style={{ color: STATUS_COLOR[run.status] ?? '#71717a', minWidth: 60, display: 'inline-block' }}>{run.status}</span>
                  <span style={{ color: '#d4d4d8', minWidth: 96, display: 'inline-block' }}>{run.pond}</span>
                  <span style={{ color: '#52525b' }}>v{run.version}</span>
                  <span style={{ color: '#71717a' }}>{duration(run)}</span>
                </div>
                {filters.ripples && run.ripples?.map((r) => <RippleRow key={r.ripple} r={r} />)}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
