'use client';

import { useLiveStore, parseTs, THEME_PULL } from '@/lib/store';
import type { PondRun, RippleRun } from '@/lib/types';
import { clock, durationOf, STATUS_COLOR } from './RunHistory';

function StatusPill({ status }: { status: string }) {
  const c = STATUS_COLOR[status] ?? '#71717a';
  return (
    <span style={{ color: c, border: `1px solid ${c}`, background: `${c}1a`, borderRadius: 4, padding: '1px 8px', fontSize: 11, fontWeight: 700, letterSpacing: '0.04em' }}>
      {status}
    </span>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ fontSize: 9, fontWeight: 700, color: '#52525b', letterSpacing: '0.1em', textTransform: 'uppercase' }}>{label}</span>
      <span style={{ fontSize: 12, color: '#d4d4d8' }}>{value}</span>
    </div>
  );
}

// Freshness F as a date+time (it's the Run's identity; may be in the future for windowed Inlets).
function freshness(iso: string): string {
  const d = new Date(parseTs(iso));
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function RippleLine({ r, isRetry }: { r: RippleRun; isRetry: boolean }) {
  const c = STATUS_COLOR[r.status] ?? '#71717a';
  const dur = durationOf(r.startedAt, r.finishedAt);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', paddingLeft: isRetry ? 16 : 0, borderBottom: '1px solid #161619' }}>
      <span style={{ width: 7, height: 7, borderRadius: '50%', background: c, flexShrink: 0 }} />
      <span style={{ flex: 1, minWidth: 0, color: isRetry ? '#a1a1aa' : '#d4d4d8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {isRetry ? <span style={{ color: THEME_PULL }}>{`↻${r.retry} `}</span> : null}{r.ripple}
      </span>
      <span style={{ color: c, fontSize: 11 }}>{r.status}</span>
      <span style={{ color: '#52525b', fontSize: 11, width: 48, textAlign: 'right' }}>{dur || '—'}</span>
      <span style={{ color: '#3f3f46', fontSize: 11, width: 64, textAlign: 'right' }}>{clock(r.finishedAt ?? r.startedAt)}</span>
    </div>
  );
}

function Empty() {
  return (
    <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20 }}>
      <span style={{ color: '#52525b', fontSize: 12, textAlign: 'center', lineHeight: 1.6 }}>
        Select a run on the left to inspect its Ripples and outcome.
      </span>
    </div>
  );
}

export function RunDetail() {
  const run: PondRun | null = useLiveStore((s) => s.selectedRun);

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: '#0c0c10', fontFamily: 'ui-monospace, SFMono-Regular, monospace', minWidth: 0 }}>
      <div style={{ padding: '5px 12px', borderBottom: '1px solid #18181d', fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
        RUN DETAIL
      </div>

      {!run ? (
        <Empty />
      ) : (
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 14px', fontSize: 12, color: '#e4e4e7' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 12 }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: '#e4e4e7' }}>{run.pond}</span>
            <span style={{ fontSize: 11, color: '#52525b' }}>v{run.version}</span>
            <span style={{ flex: 1 }} />
            <StatusPill status={run.status} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px 16px', marginBottom: 14 }}>
            <Field label="Freshness" value={freshness(run.f)} />
            <Field label="Duration" value={durationOf(run.startedAt, run.finishedAt) || '—'} />
            <Field label="Started" value={clock(run.startedAt)} />
            <Field label="Finished" value={clock(run.finishedAt)} />
          </div>

          <div style={{ fontSize: 9, fontWeight: 700, color: '#52525b', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 4 }}>
            Ripples
          </div>
          {run.ripples === undefined ? (
            <div style={{ color: '#52525b', fontSize: 11 }}>Loading…</div>
          ) : run.ripples.length === 0 ? (
            <div style={{ color: '#52525b', fontSize: 11 }}>No Ripple detail recorded for this Run.</div>
          ) : (
            run.ripples.map((r) => (
              <RippleLine key={`${r.ripple}-${r.retry}`} r={r} isRetry={r.retry > 0} />
            ))
          )}
        </div>
      )}
    </div>
  );
}
