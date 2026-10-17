'use client';

import { useState } from 'react';
import { usePlaygroundStore, formatAge, pondLabel, EXAMPLES, THEME_BRAND, THEME_DANGER } from '@/lib/store';
import { useTourStore, useTourMark } from '@/lib/tour';
import { TraceChart } from './TraceChart';
import { WindowEditor } from './WindowEditor';
import { RefreshEditor } from './RefreshEditor';

function Btn({
  onClick,
  children,
  color = THEME_BRAND,
  small = false,
  block = false,
  mark,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color?: string;
  small?: boolean;
  block?: boolean;
  mark?: { 'data-tour': string; className?: string };
}) {
  return (
    <button
      onClick={onClick}
      {...mark}
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
        width: block ? '100%' : undefined,
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

function Section({ children, mark }: { children: React.ReactNode; mark?: { 'data-tour': string; className?: string } }) {
  return (
    <div {...mark} style={{ borderTop: '1px solid #27272a', paddingTop: 14, marginTop: 14 }}>
      {children}
    </div>
  );
}

// Starts (or restarts) the guided tour.
function TourButton() {
  const requestStart = useTourStore((t) => t.requestStart);
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        requestStart();
      }}
      data-testid="tour-button"
      style={{
        background: 'transparent',
        border: `1px solid ${THEME_BRAND}`,
        color: THEME_BRAND,
        borderRadius: 5,
        padding: '2px 10px',
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: '0.06em',
        cursor: 'pointer',
        fontFamily: 'inherit',
        textTransform: 'none',
      }}
    >
      Tour
    </button>
  );
}

const exampleBtn: React.CSSProperties = {
  background: '#1e1e26',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  color: '#a1a1aa',
  padding: '4px 8px',
  fontSize: 11,
  cursor: 'pointer',
  textAlign: 'left',
  fontFamily: 'inherit',
};

const numInput: React.CSSProperties = {
  width: 64,
  background: '#1a1a1f',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  color: '#e4e4e7',
  padding: '3px 6px',
  fontSize: 12,
};

export function Sidebar({ mobile = false }: { mobile?: boolean }) {
  const ponds = usePlaygroundStore((s) => s.ponds);
  const ripples = usePlaygroundStore((s) => s.ripples);
  const pondStates = usePlaygroundStore((s) => s.pondStates);
  const now = usePlaygroundStore((s) => s.now);
  const rippleStates = usePlaygroundStore((s) => s.rippleStates);
  const selectedPondId = usePlaygroundStore((s) => s.selectedPondId);
  const selectedRippleId = usePlaygroundStore((s) => s.selectedRippleId);

  const addPond = usePlaygroundStore((s) => s.addPond);
  const addMajorVersion = usePlaygroundStore((s) => s.addMajorVersion);
  const addRipple = usePlaygroundStore((s) => s.addRipple);
  const renamePond = usePlaygroundStore((s) => s.renamePond);
  const setRippleDuration = usePlaygroundStore((s) => s.setRippleDuration);
  const renameRipple = usePlaygroundStore((s) => s.renameRipple);
  const setRippleVariability = usePlaygroundStore((s) => s.setRippleVariability);
  const setAllVariability = usePlaygroundStore((s) => s.setAllVariability);
  const loadExample = usePlaygroundStore((s) => s.loadExample);
  const linkPonds = usePlaygroundStore((s) => s.linkPonds);
  const unlinkPonds = usePlaygroundStore((s) => s.unlinkPonds);
  const linkRipples = usePlaygroundStore((s) => s.linkRipples);

  const deletePond = usePlaygroundStore((s) => s.deletePond);
  const deleteRipple = usePlaygroundStore((s) => s.deleteRipple);

  const formEpoch = usePlaygroundStore((s) => s.formEpoch);
  const sheetOpen = usePlaygroundStore((s) => s.sheetOpen);
  const setSheetOpen = usePlaygroundStore((s) => s.setSheetOpen);
  const markNewMajor = useTourMark('new-major');
  const markDuration = useTourMark('ripple-duration');
  const markVariability = useTourMark('ripple-variability');
  const markChart = useTourMark('run-chart');
  const [showAddSourcePond, setShowAddSourcePond] = useState(false);
  const [showNewMajor, setShowNewMajor] = useState(false);
  const [showAddParentRipple, setShowAddParentRipple] = useState(false);
  const [allVar, setAllVar] = useState('0');

  const selectedPond = selectedPondId ? ponds[selectedPondId] : null;
  const selectedRipple = selectedRippleId ? ripples[selectedRippleId] : null;

  const availableSourcePonds = selectedPondId
    ? Object.values(ponds).filter(
        (p) => p.id !== selectedPondId && !selectedPond?.sources.includes(p.id)
      )
    : [];

  const availableParentRipples = selectedRippleId && selectedRipple
    ? Object.values(ripples).filter(
        (r) =>
          r.pondId === selectedRipple.pondId &&
          r.id !== selectedRippleId &&
          !selectedRipple.parents.includes(r.id)
      )
    : [];

  // Mobile: the sidebar is a collapsible bottom sheet. Selecting a node opens it (in the store, so
  // the tour can open and close it too).
  const collapsed = !sheetOpen;

  const selectedLabel = selectedPondId ? pondLabel(ponds, selectedPondId) : '';
  const headerContext = selectedRipple
    ? `${selectedLabel} / ${selectedRipple.name}`
    : selectedPond ? selectedLabel : null;

  const content = (
    <>
      <Btn onClick={() => addPond()} color={THEME_BRAND}>+ Add Pond</Btn>
      {selectedPond && !selectedRipple && (
        <div style={{ fontSize: 10, color: '#52525b', marginTop: 4 }}>links as a sink of {selectedLabel}</div>
      )}

      {/* Pond selected */}
      {selectedPond && !selectedRippleId && (
        <>
          <Section>
            <Label>Pond: {selectedLabel}</Label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Name</span>
              <input type="text" defaultValue={selectedPond.name} key={`pn-${selectedPond.id}-${formEpoch}`}
                onChange={(e) => { const v = e.target.value.trim(); if (v) renamePond(selectedPond.id, v); }}
                style={{ ...numInput, width: 140 }} />
            </div>
            <div style={{ fontSize: 11, color: '#71717a', marginBottom: 10 }}>
              Runs: <span style={{ color: '#a1a1aa' }}>{pondStates[selectedPond.id]?.runsCompleted ?? 0}</span>
              <span style={{ color: '#52525b' }}> · </span>
              freshness <span style={{ color: '#a1a1aa' }}>{formatAge(pondStates[selectedPond.id]?.endF ?? 0, now)}</span> old
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              <Btn onClick={() => addRipple(selectedPond.id)} color={THEME_BRAND}>+ Add Ripple</Btn>
              <Btn onClick={() => setShowNewMajor((v) => !v)} color={THEME_BRAND} mark={markNewMajor}>+ New Major Version</Btn>
              <Btn onClick={() => deletePond(selectedPond.id)} color={THEME_DANGER}>Delete Pond</Btn>
            </div>
            {/* A new major version starts with one parent Pond, or none. */}
            {showNewMajor && (
              <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 3 }}>
                <span style={{ fontSize: 11, color: '#71717a', marginBottom: 2 }}>Parent Pond</span>
                {[null, ...Object.values(ponds).filter((p) => p.id !== selectedPond.id)].map((p) => (
                  <button key={p?.id ?? 'none'}
                    onClick={() => { addMajorVersion(selectedPond.id, p ? [p.id] : []); setShowNewMajor(false); }}
                    style={{ background: '#1e1e26', border: '1px solid #3f3f46', borderRadius: 4, color: p ? '#a1a1aa' : '#71717a', padding: '3px 8px', fontSize: 12, cursor: 'pointer', textAlign: 'left' }}>
                    {p ? pondLabel(ponds, p.id) : 'None'}
                  </button>
                ))}
              </div>
            )}

            {selectedPond.sources.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <Label>Sources</Label>
                {selectedPond.sources.map((sid) => (
                  <div key={sid} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                    <span style={{ fontSize: 12, color: '#a1a1aa' }}>{pondLabel(ponds, sid)}</span>
                    <button onClick={() => unlinkPonds(sid, selectedPond.id)} style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14 }}>✕</button>
                  </div>
                ))}
              </div>
            )}
            <div style={{ marginTop: 8 }}>
              <Btn small onClick={() => setShowAddSourcePond((v) => !v)} color={THEME_BRAND}>+ Add Source Pond</Btn>
              {showAddSourcePond && (
                <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3 }}>
                  {availableSourcePonds.length === 0 ? (
                    <span style={{ fontSize: 11, color: '#52525b' }}>No eligible ponds</span>
                  ) : (
                    availableSourcePonds.map((p) => (
                      <button key={p.id} onClick={() => { linkPonds(p.id, selectedPond.id); setShowAddSourcePond(false); }}
                        style={{ background: '#1e1e26', border: '1px solid #3f3f46', borderRadius: 4, color: '#a1a1aa', padding: '3px 8px', fontSize: 12, cursor: 'pointer', textAlign: 'left' }}>
                        {pondLabel(ponds, p.id)}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          </Section>

          {/* Windows (Inlet ponds only) */}
          {selectedPond.sources.length === 0 && (
            <Section>
              <Label>Windows (batch source)</Label>
              <WindowEditor pond={selectedPond} />
            </Section>
          )}

          {/* Refresh: refresh now, and the Pond's recurring Refresh */}
          <Section>
            <Label>Refresh</Label>
            <RefreshEditor pond={selectedPond} key={`rf-${selectedPond.id}-${formEpoch}`} />
          </Section>

          <Section mark={markChart}>
            <TraceChart
              times={pondStates[selectedPond.id]?.completionTimes ?? []}
              durations={pondStates[selectedPond.id]?.durations ?? []}
              ages={pondStates[selectedPond.id]?.ages ?? []}
            />
          </Section>
        </>
      )}

      {/* Ripple selected */}
      {selectedRipple && (
        <>
          <Section>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <Label>Ripple: {selectedRipple.name}</Label>
              <Btn small onClick={() => deleteRipple(selectedRippleId!)} color={THEME_DANGER}>Delete</Btn>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Name</span>
              <input type="text" defaultValue={selectedRipple.name} key={`n-${selectedRippleId}-${formEpoch}`}
                onChange={(e) => { const v = e.target.value.trim(); if (v) renameRipple(selectedRippleId!, v); }}
                style={{ ...numInput, width: 140 }} />
            </div>

            <div {...markDuration} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Duration</span>
              <input type="number" min="1" step="1" defaultValue={(selectedRipple.durationMs / 1000).toFixed(1)} key={`d-${selectedRippleId}-${formEpoch}`}
                onChange={(e) => { const ms = Math.max(100, parseFloat(e.target.value) * 1000); if (!isNaN(ms)) setRippleDuration(selectedRippleId!, ms); }}
                style={numInput} />
              <span style={{ fontSize: 11, color: '#71717a' }}>s</span>
            </div>

            <div {...markVariability} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Variability</span>
              <input type="number" min="0" step="0.1" defaultValue={selectedRipple.variability.toFixed(1)} key={`v-${selectedRippleId}-${formEpoch}`}
                onChange={(e) => { const v = parseFloat(e.target.value); if (!isNaN(v) && v >= 0) setRippleVariability(selectedRippleId!, v); }}
                style={numInput} />
              <span style={{ fontSize: 11, color: '#71717a' }}>σ(ln)</span>
            </div>

            <div style={{ fontSize: 11, color: '#71717a', marginBottom: 10, lineHeight: 1.6 }}>
              <div>
                Runs: <span style={{ color: '#a1a1aa' }}>{rippleStates[selectedRippleId!]?.runsCompleted ?? 0}</span>
                <span style={{ color: '#52525b' }}> · </span>
                freshness <span style={{ color: '#a1a1aa' }}>{formatAge(rippleStates[selectedRippleId!]?.endF ?? 0, now)}</span> old
              </div>
              <div>
                Last run:{' '}
                <span style={{ color: '#a1a1aa' }}>
                  {rippleStates[selectedRippleId!]?.lastDurationMs != null
                    ? `${(rippleStates[selectedRippleId!]!.lastDurationMs! / 1000).toFixed(2)}s`
                    : '—'}
                </span>
              </div>
            </div>

            {selectedRipple.parents.length > 0 && (
              <div style={{ marginBottom: 8 }}>
                <Label>Parents</Label>
                {selectedRipple.parents.map((pid) => (
                  <div key={pid} style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 3 }}>{ripples[pid]?.name ?? pid}</div>
                ))}
              </div>
            )}

            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              <Btn small onClick={() => addRipple(selectedRipple.pondId, selectedRippleId!)} color={THEME_BRAND}>+ Add Ripple</Btn>
              <Btn small onClick={() => setShowAddParentRipple((v) => !v)} color={THEME_BRAND}>+ Add Parent</Btn>
            </div>
            {showAddParentRipple && (
              <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3 }}>
                {availableParentRipples.length === 0 ? (
                  <span style={{ fontSize: 11, color: '#52525b' }}>No eligible ripples</span>
                ) : (
                  availableParentRipples.map((r) => (
                    <button key={r.id} onClick={() => { if (linkRipples(r.id, selectedRippleId!)) setShowAddParentRipple(false); }}
                      style={{ background: '#1e1e26', border: '1px solid #3f3f46', borderRadius: 4, color: '#a1a1aa', padding: '3px 8px', fontSize: 12, cursor: 'pointer', textAlign: 'left' }}>
                      {r.name}
                    </button>
                  ))
                )}
              </div>
            )}
          </Section>

          <Section mark={markChart}>
            <TraceChart
              times={rippleStates[selectedRippleId!]?.completionTimes ?? []}
              durations={rippleStates[selectedRippleId!]?.durations ?? []}
            />
          </Section>
        </>
      )}

      {/* Nothing selected: examples, global variability */}
      {!selectedPond && !selectedRipple && (
        <Section>
          <Label>Examples</Label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <button onClick={() => loadExample('demo')} style={exampleBtn}>Demo (sales pipeline)</button>
            {Object.entries(EXAMPLES).map(([key, ex]) => (
              <button key={key} onClick={() => loadExample(key)} style={exampleBtn}>{ex.label}</button>
            ))}
          </div>
        </Section>
      )}
      {!selectedPond && !selectedRipple && (
        <Section>
          <Label>Variability (all ripples)</Label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <input type="number" min="0" step="0.1" value={allVar} onChange={(e) => setAllVar(e.target.value)} style={numInput} />
            <span style={{ fontSize: 11, color: '#71717a' }}>σ(ln)</span>
            <Btn small onClick={() => { const v = parseFloat(allVar); if (!isNaN(v) && v >= 0) setAllVariability(v); }} color={THEME_BRAND}>Set</Btn>
          </div>
          <div style={{ fontSize: 10, color: '#52525b', marginTop: 6, lineHeight: 1.5 }}>
            Overwrites variability on every ripple. Each run takes duration·exp(σ·Z).
          </div>
        </Section>
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
          onClick={() => setSheetOpen(collapsed)}
          style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px', cursor: 'pointer', userSelect: 'none', flexShrink: 0 }}
        >
          <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
            {collapsed ? '▸' : '▾'} PLAYGROUND
            {headerContext && <span style={{ color: '#52525b', fontWeight: 400, marginLeft: 8 }}>{headerContext}</span>}
          </span>
          <TourButton />
        </div>
        {!collapsed && <div style={{ overflowY: 'auto', padding: '0 14px 14px' }}>{content}</div>}
      </div>
    );
  }

  return (
    <div
      className="ds-sidebar"
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
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: '#52525b', letterSpacing: '0.12em', textTransform: 'uppercase' }}>
          Playground
        </span>
        <TourButton />
      </div>
      {content}
    </div>
  );
}
