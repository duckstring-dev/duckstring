'use client';

import { useState } from 'react';
import {
  usePlaygroundStore,
  formatAge,
  THEME_BRAND,
  THEME_PULL,
  THEME_PUSH,
  THEME_SUCCESS,
  THEME_DANGER,
  THEME_BLOCKED,
  THEME_WAKE,
} from '@/lib/store';
import { TraceChart } from './TraceChart';
import { WindowEditor } from './WindowEditor';

function Btn({
  onClick,
  children,
  color = THEME_PUSH,
  small = false,
  block = false,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color?: string;
  small?: boolean;
  block?: boolean;
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

export function Sidebar({ mobile = false }: { mobile?: boolean }) {
  const ponds = usePlaygroundStore((s) => s.ponds);
  const ripples = usePlaygroundStore((s) => s.ripples);
  const pondStates = usePlaygroundStore((s) => s.pondStates);
  const now = usePlaygroundStore((s) => s.now);
  const rippleStates = usePlaygroundStore((s) => s.rippleStates);
  const selectedPondId = usePlaygroundStore((s) => s.selectedPondId);
  const selectedRippleId = usePlaygroundStore((s) => s.selectedRippleId);
  const selectedTriggerId = usePlaygroundStore((s) => s.selectedTriggerId);
  const triggers = usePlaygroundStore((s) => s.triggers);

  const addPond = usePlaygroundStore((s) => s.addPond);
  const addRipple = usePlaygroundStore((s) => s.addRipple);
  const renamePond = usePlaygroundStore((s) => s.renamePond);
  const setRippleDuration = usePlaygroundStore((s) => s.setRippleDuration);
  const renameRipple = usePlaygroundStore((s) => s.renameRipple);
  const setRippleVariability = usePlaygroundStore((s) => s.setRippleVariability);
  const setAllVariability = usePlaygroundStore((s) => s.setAllVariability);
  const linkPonds = usePlaygroundStore((s) => s.linkPonds);
  const unlinkPonds = usePlaygroundStore((s) => s.unlinkPonds);
  const linkRipples = usePlaygroundStore((s) => s.linkRipples);

  const deletePond = usePlaygroundStore((s) => s.deletePond);
  const deleteRipple = usePlaygroundStore((s) => s.deleteRipple);
  const triggerPulse = usePlaygroundStore((s) => s.triggerPulse);
  const triggerWave = usePlaygroundStore((s) => s.triggerWave);
  const triggerTide = usePlaygroundStore((s) => s.triggerTide);
  const triggerTap = usePlaygroundStore((s) => s.triggerTap);
  const removeTrigger = usePlaygroundStore((s) => s.removeTrigger);
  const force = usePlaygroundStore((s) => s.force);
  const wake = usePlaygroundStore((s) => s.wake);
  const sleep = usePlaygroundStore((s) => s.sleep);
  const kill = usePlaygroundStore((s) => s.kill);

  const [tidePeriod, setTidePeriod] = useState('2');
  const [showTideInput, setShowTideInput] = useState(false);
  const [showAddSourcePond, setShowAddSourcePond] = useState(false);
  const [showAddParentRipple, setShowAddParentRipple] = useState(false);
  const [allVar, setAllVar] = useState('0');

  const selectedPond = selectedPondId ? ponds[selectedPondId] : null;
  const selectedRipple = selectedRippleId ? ripples[selectedRippleId] : null;

  const isOutlet = selectedPondId
    ? !Object.values(ponds).some((p) => p.sources.includes(selectedPondId))
    : false;
  const trigger = selectedPondId ? triggers[selectedPondId] : undefined;

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

  // Mobile: the sidebar is a collapsible bottom sheet; selecting a node opens it
  // (state adjusted during render, per the React "you might not need an effect" pattern).
  const [collapsed, setCollapsed] = useState(true);
  const selectionKey = selectedRippleId ?? selectedTriggerId ?? selectedPondId;
  const [prevSelectionKey, setPrevSelectionKey] = useState(selectionKey);
  if (selectionKey !== prevSelectionKey) {
    setPrevSelectionKey(selectionKey);
    if (mobile && selectionKey) setCollapsed(false);
  }

  const headerContext = selectedTriggerId
    ? `${ponds[selectedTriggerId]?.name ?? ''} trigger`
    : selectedRipple
      ? `${selectedPond?.name ?? ''} / ${selectedRipple.name}`
      : selectedPond?.name ?? null;

  const content = (
    <>
      <Btn onClick={addPond} color={THEME_BRAND}>+ Add Pond</Btn>
      {selectedPond && !selectedRipple && (
        <div style={{ fontSize: 10, color: '#52525b', marginTop: 4 }}>links as a sink of {selectedPond.name}</div>
      )}

      {/* Trigger node selected */}
      {selectedTriggerId && (
        <Section>
          <Label>Trigger</Label>
          {triggers[selectedTriggerId]?.kind === 'wave' ? (
            <div style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 10 }}>Wave trigger</div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
              <span style={{ fontSize: 11, color: '#71717a' }}>max staleness</span>
              <input
                type="number"
                min="1"
                step="1"
                defaultValue={((triggers[selectedTriggerId]?.stalenessMs ?? 1000) / 1000).toFixed(1)}
                key={`tide-${selectedTriggerId}`}
                onChange={(e) => {
                  const ms = parseFloat(e.target.value) * 1000;
                  if (!isNaN(ms) && ms > 0) triggerTide(selectedTriggerId, Math.max(100, ms));
                }}
                style={numInput}
              />
              <span style={{ fontSize: 11, color: '#71717a' }}>s</span>
            </div>
          )}
          <Btn onClick={() => removeTrigger(selectedTriggerId)} color={THEME_DANGER}>Delete Trigger</Btn>
        </Section>
      )}

      {/* Pond selected */}
      {selectedPond && !selectedRippleId && (
        <>
          <Section>
            <Label>Pond: {selectedPond.name}</Label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Name</span>
              <input type="text" defaultValue={selectedPond.name} key={`pn-${selectedPond.id}`}
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
              <Btn onClick={() => deletePond(selectedPond.id)} color={THEME_DANGER}>Delete Pond</Btn>
            </div>

            {selectedPond.sources.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <Label>Sources</Label>
                {selectedPond.sources.map((sid) => (
                  <div key={sid} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                    <span style={{ fontSize: 12, color: '#a1a1aa' }}>{ponds[sid]?.name ?? sid}</span>
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
                        {p.name}
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

          {/* Trigger section */}
          <Section>
            <Label>Triggers</Label>
            {isOutlet ? (
              <>
                <div style={quadRow}>
                  <Btn block onClick={() => triggerTap(selectedPond.id)} color={THEME_PULL}>Tap</Btn>
                  <Btn block onClick={() => triggerWave(selectedPond.id)} color={THEME_PULL}>Wave</Btn>
                  <Btn block onClick={() => triggerPulse(selectedPond.id)} color={THEME_PUSH}>Pulse</Btn>
                  <Btn block onClick={() => setShowTideInput((v) => !v)} color={THEME_PUSH}>Tide</Btn>
                </div>
                {showTideInput && (
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 8 }}>
                    <span style={{ fontSize: 11, color: '#71717a' }}>max staleness</span>
                    <input type="number" min="1" step="1" value={tidePeriod} onChange={(e) => setTidePeriod(e.target.value)} style={numInput} />
                    <span style={{ fontSize: 11, color: '#71717a' }}>s</span>
                    <Btn small onClick={() => { triggerTide(selectedPond.id, Math.max(100, parseFloat(tidePeriod) * 1000)); setShowTideInput(false); }} color={THEME_PUSH}>Set</Btn>
                  </div>
                )}
              </>
            ) : (
              <div style={{ fontSize: 11, color: '#52525b' }}>Not an outlet — triggers live on outlet ponds.</div>
            )}
            {trigger && (
              <div style={{ marginTop: 8 }}>
                <Btn small onClick={() => removeTrigger(selectedPond.id)} color={THEME_DANGER}>
                  Delete {trigger.kind === 'wave' ? 'Wave' : 'Tide'} Trigger
                </Btn>
              </div>
            )}
          </Section>

          {/* Control: Force/Wake (go) and Sleep/Kill (stop) — the Pond's execution lifecycle */}
          <Section>
            <Label>Control</Label>
            <div style={quadRow}>
              <Btn block onClick={() => force(selectedPond.id)} color={THEME_SUCCESS}>Force</Btn>
              <Btn block onClick={() => wake(selectedPond.id)} color={THEME_WAKE}>Wake</Btn>
              <Btn block onClick={() => sleep(selectedPond.id)} color={THEME_BLOCKED}>Sleep</Btn>
              <Btn block onClick={() => kill(selectedPond.id)} color={THEME_DANGER}>Kill</Btn>
            </div>
            <div style={{ fontSize: 10, color: '#52525b', marginTop: 6, lineHeight: 1.5 }}>
              Force: recompute now at the current freshness. Wake: run once if Sources are fresher
              (no upstream pull); clears a kill. Sleep: clear demand and the standing trigger.
              Kill: cancel the run and park the Pond until a Wake/Force.
            </div>
            <div style={{ marginTop: 6 }}>
              <Btn small onClick={() => sleep(selectedPond.id, true)} color={THEME_BLOCKED}>Sleep Upstream</Btn>
            </div>
          </Section>

          <Section>
            <TraceChart
              times={pondStates[selectedPond.id]?.completionTimes ?? []}
              durations={pondStates[selectedPond.id]?.durations ?? []}
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
              <input type="text" defaultValue={selectedRipple.name} key={`n-${selectedRippleId}`}
                onChange={(e) => { const v = e.target.value.trim(); if (v) renameRipple(selectedRippleId!, v); }}
                style={{ ...numInput, width: 140 }} />
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Duration</span>
              <input type="number" min="1" step="1" defaultValue={(selectedRipple.durationMs / 1000).toFixed(1)} key={`d-${selectedRippleId}`}
                onChange={(e) => { const ms = Math.max(100, parseFloat(e.target.value) * 1000); if (!isNaN(ms)) setRippleDuration(selectedRippleId!, ms); }}
                style={numInput} />
              <span style={{ fontSize: 11, color: '#71717a' }}>s</span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Variability</span>
              <input type="number" min="0" step="0.1" defaultValue={selectedRipple.variability.toFixed(1)} key={`v-${selectedRippleId}`}
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

          <Section>
            <TraceChart
              times={rippleStates[selectedRippleId!]?.completionTimes ?? []}
              durations={rippleStates[selectedRippleId!]?.durations ?? []}
            />
          </Section>
        </>
      )}

      {/* Nothing selected: global variability */}
      {!selectedPond && !selectedRipple && !selectedTriggerId && (
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
          onClick={() => setCollapsed((v) => !v)}
          style={{ display: 'flex', alignItems: 'center', padding: '8px 14px', cursor: 'pointer', userSelect: 'none', flexShrink: 0 }}
        >
          <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
            {collapsed ? '▸' : '▾'} PLAYGROUND
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
      <div style={{ fontSize: 11, fontWeight: 700, color: '#52525b', letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 16 }}>
        Playground
      </div>
      {content}
    </div>
  );
}
