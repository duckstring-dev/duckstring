'use client';

import { useState } from 'react';
import { useLiveStore, formatAge, parseTs } from '@/lib/store';
import type { PondRun } from '@/lib/types';
import { TraceChart } from './TraceChart';
import { WindowEditor } from './WindowEditor';

function Btn({
  onClick,
  children,
  color = '#3b82f6',
  small = false,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color?: string;
  small?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        background: 'transparent',
        border: `1px solid ${color}`,
        color,
        borderRadius: 5,
        padding: small ? '2px 8px' : '5px 12px',
        fontSize: small ? 11 : 12,
        cursor: 'pointer',
        fontWeight: 600,
        letterSpacing: '0.04em',
      }}
    >
      {children}
    </button>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 700, color: '#52525b', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
      {children}
    </div>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return <div style={{ borderTop: '1px solid #27272a', paddingTop: 14, marginTop: 14 }}>{children}</div>;
}

const numInput: React.CSSProperties = {
  width: 64,
  background: '#1a1a1f',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  color: '#e4e4e7',
  padding: '3px 6px',
  fontSize: 12,
};

const ms = (iso: string | null): number => parseTs(iso);

// Completion times (asc, ms) and per-run durations (ms) for a set of Pond Runs.
function pondTrace(runs: PondRun[]): { times: number[]; durations: number[] } {
  const asc = [...runs].reverse(); // store holds newest-first
  const times: number[] = [];
  const durations: number[] = [];
  for (const r of asc) {
    if (r.finishedAt) times.push(ms(r.finishedAt));
    if (r.startedAt && r.finishedAt) durations.push(ms(r.finishedAt) - ms(r.startedAt));
  }
  return { times, durations };
}

// Ripple completion times (asc, ms) for cadence. Ripple Runs don't record a start, so no durations.
function rippleTrace(runs: PondRun[], rippleName: string): { times: number[]; durations: number[] } {
  const asc = [...runs].reverse();
  const times: number[] = [];
  for (const r of asc) {
    const rr = r.ripples?.find((x) => x.ripple === rippleName);
    if (rr?.finishedAt) times.push(ms(rr.finishedAt));
  }
  return { times, durations: [] };
}

export function Sidebar() {
  const ponds = useLiveStore((s) => s.ponds);
  const ripples = useLiveStore((s) => s.ripples);
  const pondViews = useLiveStore((s) => s.pondViews);
  const rippleViews = useLiveStore((s) => s.rippleViews);
  const pondInfo = useLiveStore((s) => s.pondInfo);
  const triggers = useLiveStore((s) => s.triggers);
  const now = useLiveStore((s) => s.now);
  const selectedPondRuns = useLiveStore((s) => s.selectedPondRuns);

  const selectedPondId = useLiveStore((s) => s.selectedPondId);
  const selectedRippleId = useLiveStore((s) => s.selectedRippleId);
  const selectedTriggerId = useLiveStore((s) => s.selectedTriggerId);

  const tap = useLiveStore((s) => s.tap);
  const pulse = useLiveStore((s) => s.pulse);
  const wave = useLiveStore((s) => s.wave);
  const tide = useLiveStore((s) => s.tide);
  const start = useLiveStore((s) => s.start);
  const stop = useLiveStore((s) => s.stop);
  const removeTrigger = useLiveStore((s) => s.removeTrigger);

  const [tideBound, setTideBound] = useState('2');
  const [showTideInput, setShowTideInput] = useState(false);

  const selectedPond = selectedPondId ? ponds[selectedPondId] : null;
  const selectedRipple = selectedRippleId ? ripples[selectedRippleId] : null;
  const trigger = selectedPondId ? triggers[selectedPondId] : undefined;
  const isInlet = selectedPond ? selectedPond.sources.length === 0 : false;

  return (
    <div
      style={{
        width: 290,
        minWidth: 290,
        background: '#15151a',
        borderLeft: '1px solid #27272a',
        padding: 18,
        display: 'flex',
        flexDirection: 'column',
        overflowY: 'auto',
        color: '#e4e4e7',
        fontFamily: 'inherit',
      }}
    >
      <div style={{ fontSize: 11, fontWeight: 700, color: '#52525b', letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 8 }}>
        Duckstring
      </div>

      {!selectedPond && !selectedRipple && !selectedTriggerId && (
        <div style={{ fontSize: 12, color: '#71717a', lineHeight: 1.6 }}>
          Select a Pond or Ripple to inspect its freshness and run history, or to send a Tap, Pulse,
          Wave, or Tide. Ponds are established by deploying code — not from here.
        </div>
      )}

      {/* Trigger node selected */}
      {selectedTriggerId && triggers[selectedTriggerId] && (
        <Section>
          <Label>Trigger · {selectedTriggerId}</Label>
          {triggers[selectedTriggerId].kind === 'wave' ? (
            <div style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 10 }}>Wave — standing pull.</div>
          ) : (
            <div style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 10 }}>
              Tide — max staleness ≤ {((triggers[selectedTriggerId].boundMs ?? 1000) / 1000).toFixed(1)}s.
            </div>
          )}
          <Btn onClick={() => removeTrigger(selectedTriggerId)} color="#ef4444">Remove Trigger</Btn>
        </Section>
      )}

      {/* Pond selected */}
      {selectedPond && !selectedRippleId && (
        <>
          <Section>
            <Label>Pond: {selectedPond.name}</Label>
            <div style={{ fontSize: 11, color: '#71717a', marginBottom: 8, lineHeight: 1.7 }}>
              <div>
                {pondInfo[selectedPond.id]?.kind ?? 'pond'}
                <span style={{ color: '#52525b' }}> · </span>
                v<span style={{ color: '#a1a1aa' }}>{pondInfo[selectedPond.id]?.version ?? '—'}</span>
              </div>
              <div>
                Runs: <span style={{ color: '#a1a1aa' }}>{pondViews[selectedPond.id]?.runsCompleted ?? 0}</span>
                <span style={{ color: '#52525b' }}> · </span>
                fresh <span style={{ color: '#a1a1aa' }}>{formatAge(pondViews[selectedPond.id]?.endF ?? 0, now)}</span> old
              </div>
            </div>

            {selectedPond.sources.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <Label>Sources</Label>
                {selectedPond.sources.map((sid) => (
                  <div key={sid} style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 3 }}>{ponds[sid]?.name ?? sid}</div>
                ))}
              </div>
            )}
          </Section>

          {/* Windows (Inlet ponds only) */}
          {isInlet && (
            <Section>
              <Label>Windows (batch source)</Label>
              <WindowEditor pond={selectedPond} />
            </Section>
          )}

          {/* Triggers */}
          <Section>
            <Label>Triggers</Label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              <Btn onClick={() => tap(selectedPond.id)} color="#22c55e">Tap</Btn>
              <Btn onClick={() => wave(selectedPond.id)} color="#22c55e">Wave</Btn>
              <Btn onClick={() => pulse(selectedPond.id)} color="#3b82f6">Pulse</Btn>
              <Btn onClick={() => setShowTideInput((v) => !v)} color="#3b82f6">Tide</Btn>
            </div>
            {showTideInput && (
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 8 }}>
                <span style={{ fontSize: 11, color: '#71717a' }}>max staleness</span>
                <input type="number" min="1" step="1" value={tideBound} onChange={(e) => setTideBound(e.target.value)} style={numInput} />
                <span style={{ fontSize: 11, color: '#71717a' }}>s</span>
                <Btn small onClick={() => { tide(selectedPond.id, Math.max(0.1, parseFloat(tideBound))); setShowTideInput(false); }} color="#3b82f6">Set</Btn>
              </div>
            )}
            {trigger && (
              <div style={{ marginTop: 8 }}>
                <Btn small onClick={() => removeTrigger(selectedPond.id)} color="#ef4444">
                  Remove {trigger.kind === 'wave' ? 'Wave' : 'Tide'} Trigger
                </Btn>
              </div>
            )}
          </Section>

          {/* Control: start a one-off run, or clear demand (this Pond, or its whole lineage) */}
          <Section>
            <Label>Control</Label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              <Btn onClick={() => start(selectedPond.id)} color="#22c55e">Start</Btn>
              <Btn onClick={() => stop(selectedPond.id)} color="#ef4444">Stop</Btn>
              <Btn onClick={() => stop(selectedPond.id, true)} color="#ef4444">Stop Lineage</Btn>
            </div>
            <div style={{ fontSize: 10, color: '#52525b', marginTop: 6, lineHeight: 1.5 }}>
              Start: one run on this Pond, no upstream. Stop: clear this Pond&apos;s demand. Stop Lineage: also clear all upstream sources.
            </div>
          </Section>

          <Section>
            <TraceChart {...pondTrace(selectedPondRuns)} />
          </Section>
        </>
      )}

      {/* Ripple selected */}
      {selectedRipple && (
        <>
          <Section>
            <Label>Ripple: {selectedRipple.name}</Label>
            <div style={{ fontSize: 11, color: '#71717a', marginBottom: 8, lineHeight: 1.7 }}>
              <div>in <span style={{ color: '#a1a1aa' }}>{ponds[selectedRipple.pondId]?.name ?? selectedRipple.pondId}</span></div>
              <div>
                Runs: <span style={{ color: '#a1a1aa' }}>{rippleViews[selectedRipple.id]?.runsCompleted ?? 0}</span>
                <span style={{ color: '#52525b' }}> · </span>
                fresh <span style={{ color: '#a1a1aa' }}>{formatAge(rippleViews[selectedRipple.id]?.endF ?? 0, now)}</span> old
              </div>
            </div>

            {selectedRipple.parents.length > 0 && (
              <div style={{ marginBottom: 4 }}>
                <Label>Parents</Label>
                {selectedRipple.parents.map((pid) => (
                  <div key={pid} style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 3 }}>{ripples[pid]?.name ?? pid}</div>
                ))}
              </div>
            )}
          </Section>

          <Section>
            <TraceChart {...rippleTrace(selectedPondRuns, selectedRipple.name)} />
          </Section>
        </>
      )}
    </div>
  );
}
