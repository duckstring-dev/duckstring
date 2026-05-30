'use client';

import { useState } from 'react';
import { usePlaygroundStore } from '@/lib/store';
import { TraceChart } from './TraceChart';

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

export function Sidebar() {
  const ponds = usePlaygroundStore((s) => s.ponds);
  const ripples = usePlaygroundStore((s) => s.ripples);
  const pondStates = usePlaygroundStore((s) => s.pondStates);
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
  const triggerStop = usePlaygroundStore((s) => s.triggerStop);
  const triggerStart = usePlaygroundStore((s) => s.triggerStart);
  const removeTrigger = usePlaygroundStore((s) => s.removeTrigger);

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
      <div style={{ fontSize: 11, fontWeight: 700, color: '#52525b', letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 16 }}>
        Playground
      </div>

      <Btn onClick={addPond} color="#6366f1">+ Add Pond</Btn>
      {selectedPond && !selectedRipple && (
        <div style={{ fontSize: 10, color: '#52525b', marginTop: 4 }}>links as a sink of {selectedPond.name}</div>
      )}

      {/* Trigger node selected */}
      {selectedTriggerId && (
        <Section>
          <Label>Trigger</Label>
          <div style={{ fontSize: 12, color: '#a1a1aa', marginBottom: 10 }}>
            {triggers[selectedTriggerId]?.kind === 'wave'
              ? 'Wave trigger'
              : `Tide trigger (${((triggers[selectedTriggerId]?.periodMs ?? 1000) / 1000).toFixed(1)}s)`}
          </div>
          <Btn onClick={() => removeTrigger(selectedTriggerId)} color="#ef4444">Delete Trigger</Btn>
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
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              <Btn onClick={() => addRipple(selectedPond.id)} color="#6366f1">+ Add Ripple</Btn>
              <Btn onClick={() => deletePond(selectedPond.id)} color="#ef4444">Delete Pond</Btn>
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
              <Btn small onClick={() => setShowAddSourcePond((v) => !v)} color="#6366f1">+ Add Source Pond</Btn>
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

          {/* Trigger section */}
          <Section>
            <Label>Triggers</Label>
            {isOutlet ? (
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  <Btn onClick={() => triggerPulse(selectedPond.id)} color="#3b82f6">Pulse</Btn>
                  <Btn onClick={() => triggerWave(selectedPond.id)} color="#22c55e">Wave</Btn>
                  <Btn onClick={() => setShowTideInput((v) => !v)} color="#3b82f6">Tide</Btn>
                </div>
                {showTideInput && (
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 8 }}>
                    <input type="number" min="0.1" step="0.5" value={tidePeriod} onChange={(e) => setTidePeriod(e.target.value)} style={numInput} />
                    <span style={{ fontSize: 11, color: '#71717a' }}>s</span>
                    <Btn small onClick={() => { triggerTide(selectedPond.id, Math.max(100, parseFloat(tidePeriod) * 1000)); setShowTideInput(false); }} color="#3b82f6">Set</Btn>
                  </div>
                )}
              </>
            ) : (
              <div style={{ fontSize: 11, color: '#52525b' }}>Not an outlet — triggers live on outlet ponds.</div>
            )}
            {trigger && (
              <div style={{ marginTop: 8 }}>
                <Btn small onClick={() => removeTrigger(selectedPond.id)} color="#ef4444">
                  Delete {trigger.kind === 'wave' ? 'Wave' : 'Tide'} Trigger
                </Btn>
              </div>
            )}
          </Section>

          {/* Start section */}
          <Section>
            <Label>Start</Label>
            <Btn onClick={() => triggerStart(selectedPond.id)} color="#22c55e">Start</Btn>
          </Section>

          {/* Stop section */}
          <Section>
            <Label>Stop</Label>
            <Btn onClick={() => triggerStop(selectedPond.id)} color="#ef4444">Stop</Btn>
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
              <Btn small onClick={() => deleteRipple(selectedRippleId!)} color="#ef4444">Delete</Btn>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Name</span>
              <input type="text" defaultValue={selectedRipple.name} key={`n-${selectedRippleId}`}
                onChange={(e) => { const v = e.target.value.trim(); if (v) renameRipple(selectedRippleId!, v); }}
                style={{ ...numInput, width: 140 }} />
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: '#71717a', width: 64 }}>Duration</span>
              <input type="number" min="0.1" step="0.5" defaultValue={(selectedRipple.durationMs / 1000).toFixed(1)} key={`d-${selectedRippleId}`}
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

            <div style={{ fontSize: 11, color: '#71717a', marginBottom: 10 }}>
              Last run:{' '}
              <span style={{ color: '#a1a1aa' }}>
                {rippleStates[selectedRippleId!]?.lastDurationMs != null
                  ? `${(rippleStates[selectedRippleId!]!.lastDurationMs! / 1000).toFixed(2)}s`
                  : '—'}
              </span>
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
              <Btn small onClick={() => addRipple(selectedRipple.pondId, selectedRippleId!)} color="#6366f1">+ Add Ripple</Btn>
              <Btn small onClick={() => setShowAddParentRipple((v) => !v)} color="#6366f1">+ Add Parent</Btn>
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
            <Btn small onClick={() => { const v = parseFloat(allVar); if (!isNaN(v) && v >= 0) setAllVariability(v); }} color="#6366f1">Set</Btn>
          </div>
          <div style={{ fontSize: 10, color: '#52525b', marginTop: 6, lineHeight: 1.5 }}>
            Overwrites variability on every ripple. Each run takes duration·exp(σ·Z).
          </div>
        </Section>
      )}
    </div>
  );
}
