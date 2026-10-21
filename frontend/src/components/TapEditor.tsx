'use client';

import { useEffect, useState } from 'react';
import { addTap, removeTap, rotateTap, UnauthorizedError } from '@/lib/api';
import { useLiveStore, formatAge, THEME_AMBER, THEME_BRAND, THEME_DANGER } from '@/lib/store';
import type { FreqUnit, Pond, TapView } from '@/lib/types';
import { ConfirmDialog, type ConfirmOpts } from './ConfirmDialog';
import { AddButton, UNITS, UNIT_SECONDS, numInput, selectInput } from './recurrence';

// A Pond's Taps, at the foot of its Refresh section: the UI for `duckstring refresh tap add|ls|rm|rotate POND`
// (full access, since a Tap is a credential). A Tap refreshes the Pond when an app queries it through the Tap's
// own key. The section lists names only; adding one, and a Tap's details (policy, use, Rotate, Delete), open
// in a modal. A key is shown once, when added or rotated.

const NO_TAPS: TapView[] = [];
const KEY_NOTE = 'Copy this key now - it will only be shown once.';

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function duration(ms: number): string {
  const s = Math.round(ms / 1000);
  for (const [unit, name] of [[604800, 'wk'], [86400, 'day'], [3600, 'hr'], [60, 'min']] as const) {
    if (s >= unit && s % unit === 0) return `${s / unit} ${name}`;
  }
  return `${s} sec`;
}

// A themed modal shell: Escape or a click outside closes it.
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1500, display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(9, 9, 11, 0.72)', padding: 16, fontFamily: 'ui-monospace, SFMono-Regular, monospace',
      }}
    >
      <div
        role="dialog"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#101014', border: '1px solid #3f3f46', borderRadius: 10, width: 'min(420px, 92vw)',
          padding: '16px 18px', boxShadow: '0 10px 40px rgba(0,0,0,0.5)', display: 'flex', flexDirection: 'column', gap: 8,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 13.5, fontWeight: 700, color: '#e4e4e7' }}>{title}</span>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{ background: 'none', border: '1px solid #3f3f46', borderRadius: 5, color: '#a1a1aa', cursor: 'pointer', padding: '2px 8px' }}
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function KeyBox({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ border: `1px solid ${THEME_AMBER}`, background: `${THEME_AMBER}14`, borderRadius: 6, padding: '8px 10px' }}>
      <div style={{ fontSize: 11, color: THEME_AMBER, fontWeight: 700, marginBottom: 6 }}>{KEY_NOTE}</div>
      <code style={{ fontSize: 11, color: '#fafafa', wordBreak: 'break-all', display: 'block', marginBottom: 8 }}>{value}</code>
      <AddButton onClick={() => navigator.clipboard?.writeText(value).then(() => setCopied(true), () => setCopied(false))}>
        {copied ? 'Copied' : 'Copy'}
      </AddButton>
    </div>
  );
}

const row: React.CSSProperties = { display: 'flex', gap: 6, alignItems: 'center' };
const lab: React.CSSProperties = { fontSize: 11, color: '#71717a', width: 88, flexShrink: 0 };

// Runs a Tap call: refreshes the store after, and sends a 401 to the key prompt.
function useRun() {
  const refresh = useLiveStore((s) => s.refresh);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setErr(null);
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      if (e instanceof UnauthorizedError) useLiveStore.setState({ needsKey: true });
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, err, run };
}

function AddTapModal({ pond, onClose }: { pond: Pond; onClose: () => void }) {
  const [name, setName] = useState('');
  const [fresh, setFresh] = useState('0');
  const [freshUnit, setFreshUnit] = useState<FreqUnit>('MINUTE');
  const [key, setKey] = useState<string | null>(null);
  const { busy, err, run } = useRun();
  const fld: React.CSSProperties = { ...numInput, width: '100%', boxSizing: 'border-box' };

  const add = () =>
    run(async () => {
      const resp = await addTap(pond.id, {
        name: name.trim(),
        fresh_within_ms: Math.max(0, parseInt(fresh, 10) || 0) * UNIT_SECONDS[freshUnit] * 1000,
      });
      setKey(resp.key);
    });

  return (
    <Modal title={key ? `Tap '${name.trim()}' added` : 'Add a Tap'} onClose={onClose}>
      {key ? (
        <KeyBox value={key} />
      ) : (
        <>
          <div style={row}>
            <span style={lab}>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. dashboard" style={fld} />
          </div>
          <div style={row}>
            <span style={lab}>Fresh within</span>
            <input type="number" min="0" value={fresh} onChange={(e) => setFresh(e.target.value)} style={numInput} />
            <select value={freshUnit} onChange={(e) => setFreshUnit(e.target.value as FreqUnit)} style={selectInput}>
              {UNITS.map((u) => <option key={u.value} value={u.value}>{u.label}</option>)}
            </select>
          </div>
          {err && <div style={{ fontSize: 11, color: THEME_DANGER, wordBreak: 'break-word' }}>{err}</div>}
          <div>
            <AddButton onClick={add} disabled={busy || !name.trim()}>{busy ? 'Adding…' : 'Add'}</AddButton>
          </div>
        </>
      )}
    </Modal>
  );
}

function TapDetailModal({ pond, tap, onClose }: { pond: Pond; tap: TapView; onClose: () => void }) {
  const now = useLiveStore((s) => s.now);
  const [key, setKey] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmOpts | null>(null);
  const { err, run } = useRun();
  const line = (label: string, value: string) => (
    <div style={row}>
      <span style={lab}>{label}</span>
      <span style={{ fontSize: 12, color: '#d4d4d8' }}>{value}</span>
    </div>
  );

  return (
    <Modal title={tap.name} onClose={onClose}>
      {line('Fresh within', tap.freshWithinMs ? duration(tap.freshWithinMs) : 'every query')}
      {line('Last query', tap.lastQueryAt ? `${formatAge(tap.lastQueryAt, now)} ago` : 'never')}
      {line('Use', `${plural(tap.queryCount, 'query', 'queries')}, ${plural(tap.refreshCount, 'refresh', 'refreshes')}`)}
      {key && <KeyBox value={key} />}
      {err && <div style={{ fontSize: 11, color: THEME_DANGER, wordBreak: 'break-word' }}>{err}</div>}
      <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
        <AddButton
          onClick={() => setConfirm({
            title: `Replace key for ${tap.name}?`, body: '', confirmLabel: 'Replace',
            action: () => run(async () => setKey((await rotateTap(pond.id, tap.name)).key)),
          })}
        >
          Rotate
        </AddButton>
        <AddButton
          color={THEME_DANGER}
          onClick={() => setConfirm({
            title: `Delete key for ${tap.name}?`, body: '', confirmLabel: 'Delete',
            action: () => run(async () => {
              await removeTap(pond.id, tap.name);
              onClose();
            }),
          })}
        >
          Delete
        </AddButton>
      </div>
      {confirm && <ConfirmDialog opts={confirm} onClose={() => setConfirm(null)} />}
    </Modal>
  );
}

export function TapList({ pond }: { pond: Pond }) {
  const taps = useLiveStore((s) => s.taps[pond.id]) ?? NO_TAPS;
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const openTap = taps.find((t) => t.name === open);

  return (
    <div style={{ marginTop: 10 }}>
      <AddButton block color={THEME_BRAND} onClick={() => setAdding(true)}>Taps (Refresh on Request)</AddButton>
      <div style={{ marginTop: 6 }}>
        {taps.map((t) => (
          <div
            key={t.name}
            role="button"
            onClick={() => setOpen(t.name)}
            style={{ fontSize: 12, color: '#a1a1aa', cursor: 'pointer', marginBottom: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          >
            {t.name}
          </div>
        ))}
      </div>
      {adding && <AddTapModal pond={pond} onClose={() => setAdding(false)} />}
      {openTap && <TapDetailModal pond={pond} tap={openTap} onClose={() => setOpen(null)} />}
    </div>
  );
}
