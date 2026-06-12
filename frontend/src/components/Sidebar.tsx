'use client';

import { useState } from 'react';
import { useLiveStore, formatAge, formatDuration, parseTs, THEME_PULL, THEME_PUSH, THEME_SUCCESS, THEME_DANGER, THEME_BLOCKED, THEME_WAKE } from '@/lib/store';
import type { FreqUnit, PondRun } from '@/lib/types';
import { TraceChart } from './TraceChart';
import { WindowEditor } from './WindowEditor';

function Btn({
  onClick,
  children,
  color = THEME_PUSH,
  small = false,
  disabled = false,
  block = false,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color?: string;
  small?: boolean;
  disabled?: boolean;
  block?: boolean;
}) {
  return (
    <button
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      style={{
        background: 'transparent',
        border: `1px solid ${color}`,
        color,
        borderRadius: 5,
        padding: small ? '2px 8px' : '5px 12px',
        fontSize: small ? 11 : 12,
        cursor: disabled ? 'not-allowed' : 'pointer',
        fontWeight: 600,
        letterSpacing: '0.04em',
        opacity: disabled ? 0.35 : 1,
        width: block ? '100%' : undefined,
      }}
    >
      {children}
    </button>
  );
}

// A 4-column row of equal-width buttons — so the Trigger and Control rows line up.
const quadRow: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6 };

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

const selectInput: React.CSSProperties = { ...numInput, width: 'auto' };

const TIDE_UNITS: { value: FreqUnit; label: string; secs: number }[] = [
  { value: 'SECOND', label: 'sec', secs: 1 },
  { value: 'MINUTE', label: 'min', secs: 60 },
  { value: 'HOUR', label: 'hr', secs: 3600 },
  { value: 'DAY', label: 'day', secs: 86400 },
  { value: 'WEEK', label: 'wk', secs: 604800 },
];

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

// Ripple completion times (asc, ms) and per-run durations (ms) from the selected Pond's run history.
function rippleTrace(runs: PondRun[], rippleName: string): { times: number[]; durations: number[] } {
  const asc = [...runs].reverse();
  const times: number[] = [];
  const durations: number[] = [];
  for (const r of asc) {
    const rr = r.ripples?.find((x) => x.ripple === rippleName);
    if (rr?.finishedAt) times.push(ms(rr.finishedAt));
    if (rr?.startedAt && rr?.finishedAt) durations.push(ms(rr.finishedAt) - ms(rr.startedAt));
  }
  return { times, durations };
}

export function Sidebar({ mobile = false }: { mobile?: boolean }) {
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
  const wake = useLiveStore((s) => s.wake);
  const sleep = useLiveStore((s) => s.sleep);
  const force = useLiveStore((s) => s.force);
  const kill = useLiveStore((s) => s.kill);
  const removeTrigger = useLiveStore((s) => s.removeTrigger);
  const clearFailure = useLiveStore((s) => s.clearFailure);
  const setBudget = useLiveStore((s) => s.setBudget);

  const [tideBound, setTideBound] = useState('2');
  const [tideUnit, setTideUnit] = useState<FreqUnit>('SECOND');
  const [showTideInput, setShowTideInput] = useState(false);

  // Retry-budget inputs, seeded from the selected Pond's live config. Re-seed only when the selection
  // changes (the "adjust state while rendering" pattern) so live polls don't clobber typing mid-edit.
  const [immRetries, setImmRetries] = useState('0');
  const [srcRetries, setSrcRetries] = useState('0');
  const [budgetPond, setBudgetPond] = useState<string | null>(null);
  if (selectedPondId !== budgetPond) {
    setBudgetPond(selectedPondId);
    const info = selectedPondId ? pondInfo[selectedPondId] : null;
    setImmRetries(info ? String(info.immediateRetries) : '0');
    setSrcRetries(info ? String(info.sourceRetries) : '0');
  }

  const selectedPond = selectedPondId ? ponds[selectedPondId] : null;
  const selectedRipple = selectedRippleId ? ripples[selectedRippleId] : null;
  const trigger = selectedPondId ? triggers[selectedPondId] : undefined;
  const isInlet = selectedPond ? selectedPond.sources.length === 0 : false;

  // Mobile: the sidebar is a collapsible bottom sheet; selecting a node opens it
  // (state adjusted during render, like the budget inputs above).
  const [collapsed, setCollapsed] = useState(true);
  const selectionKey = selectedRippleId ?? selectedTriggerId ?? selectedPondId;
  const [prevSelectionKey, setPrevSelectionKey] = useState(selectionKey);
  if (selectionKey !== prevSelectionKey) {
    setPrevSelectionKey(selectionKey);
    if (mobile && selectionKey) setCollapsed(false);
  }

  const headerContext = selectedTriggerId
    ? `${ponds[selectedTriggerId]?.name ?? selectedTriggerId} trigger`
    : selectedRipple
      ? `${ponds[selectedRipple.pondId]?.name ?? ''} / ${selectedRipple.name}`
      : selectedPond?.name ?? null;

  const content = (
    <>
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
              Tide — max staleness ≤ {formatDuration(triggers[selectedTriggerId].boundMs ?? 1000)}.
            </div>
          )}
          <Btn onClick={() => removeTrigger(selectedTriggerId)} color={THEME_DANGER}>Remove Trigger</Btn>
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
            <div style={quadRow}>
              <Btn block onClick={() => tap(selectedPond.id)} color={THEME_PULL}>Tap</Btn>
              <Btn block onClick={() => wave(selectedPond.id)} color={THEME_PULL}>Wave</Btn>
              <Btn block onClick={() => pulse(selectedPond.id)} color={THEME_PUSH}>Pulse</Btn>
              <Btn block onClick={() => setShowTideInput((v) => !v)} color={THEME_PUSH}>Tide</Btn>
            </div>
            {showTideInput && (
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 8 }}>
                <span style={{ fontSize: 11, color: '#71717a' }} title="keep no more stale than">≤</span>
                <input type="number" min="1" step="1" value={tideBound} onChange={(e) => setTideBound(e.target.value)} style={{ ...numInput, width: 56 }} />
                <select value={tideUnit} onChange={(e) => setTideUnit(e.target.value as FreqUnit)} style={selectInput}>
                  {TIDE_UNITS.map((u) => (
                    <option key={u.value} value={u.value}>{u.label}</option>
                  ))}
                </select>
                <Btn
                  small
                  onClick={() => {
                    const secs = TIDE_UNITS.find((u) => u.value === tideUnit)!.secs;
                    tide(selectedPond.id, Math.max(0.1, parseFloat(tideBound) * secs));
                    setShowTideInput(false);
                  }}
                  color={THEME_PUSH}
                >
                  Set
                </Btn>
              </div>
            )}
            {trigger && (
              <div style={{ marginTop: 8 }}>
                <Btn small onClick={() => removeTrigger(selectedPond.id)} color={THEME_DANGER}>
                  Remove {trigger.kind === 'wave' ? 'Wave' : 'Tide'} Trigger
                </Btn>
              </div>
            )}
          </Section>

          {/* Control: Force/Wake (go) and Sleep/Kill (stop) lifecycle on the Duck */}
          <Section>
            <Label>Control</Label>
            <div style={quadRow}>
              <Btn block onClick={() => force(selectedPond.id)} color={THEME_SUCCESS}>Force</Btn>
              <Btn block onClick={() => wake(selectedPond.id)} color={THEME_WAKE}>Wake</Btn>
              <Btn block onClick={() => sleep(selectedPond.id)} color={THEME_BLOCKED}>Sleep</Btn>
              <Btn block onClick={() => kill(selectedPond.id)} color={THEME_DANGER}>Kill</Btn>
            </div>
          </Section>

          {/* Failures: retry budgets + clearing a failed Pond */}
          <Section>
            <Label>Failures</Label>
            {([
              ['Immediate Retries', immRetries, setImmRetries],
              ['On Change Retries', srcRetries, setSrcRetries],
            ] as const).map(([label, value, setValue]) => (
              <div key={label} style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                <span style={{ flex: 1, fontSize: 11, color: '#a1a1aa' }}>{label}</span>
                <input type="number" min="0" step="1" value={value} onChange={(e) => setValue(e.target.value)} style={{ ...numInput, width: 48 }} />
                <Btn
                  small
                  color={THEME_PUSH}
                  onClick={() => setBudget(selectedPond.id, Math.max(0, parseInt(immRetries) || 0), Math.max(0, parseInt(srcRetries) || 0))}
                >
                  Set
                </Btn>
              </div>
            ))}
            {pondInfo[selectedPond.id]?.isFailed && (
              <div style={{ marginTop: 8 }}>
                <Btn onClick={() => clearFailure(selectedPond.id)} color={THEME_SUCCESS}>Clear Failure</Btn>
              </div>
            )}
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
    </>
  );

  if (mobile) {
    return (
      <div
        className="ds-sidebar"
        style={{
          background: '#15151a',
          borderTop: '1px solid #27272a',
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          maxHeight: '46dvh',
          color: '#e4e4e7',
          fontFamily: 'inherit',
        }}
      >
        <div
          onClick={() => setCollapsed((v) => !v)}
          style={{ display: 'flex', alignItems: 'center', padding: '8px 14px', cursor: 'pointer', userSelect: 'none', flexShrink: 0 }}
        >
          <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
            {collapsed ? '▸' : '▾'} CATCHMENT
            {headerContext && <span style={{ color: '#52525b', fontWeight: 400, marginLeft: 8 }}>{headerContext}</span>}
          </span>
        </div>
        {!collapsed && <div style={{ overflowY: 'auto', padding: '0 14px 14px' }}>{content}</div>}
      </div>
    );
  }

  return (
    <div
      className="ds-sidebar"
      style={{
        width: 320,
        minWidth: 320,
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
      {content}
    </div>
  );
}
