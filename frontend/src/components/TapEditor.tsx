'use client';

import { useState } from 'react';
import { addTap, removeTap, rotateTap, UnauthorizedError } from '@/lib/api';
import { useLiveStore, formatAge, THEME_BRAND, THEME_DANGER, THEME_AMBER } from '@/lib/store';
import type { FreqUnit, Pond, TapView } from '@/lib/types';
import { AddButton, UNITS, UNIT_SECONDS, numInput, selectInput } from './recurrence';

// A Pond's Taps: the UI for `duckstring tap add|ls|rm|rotate POND` (full access: a Tap is a credential). A
// Tap is an app's access point: its own key, which reads only the Tap's tables, and a policy. A query through
// it refreshes the Pond when the newest request or the data is at least "fresh within" old (0 = every query),
// and "wait" holds the query until the data is that fresh. A key is shown once, when added or rotated.

const NO_TAPS: TapView[] = [];

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function duration(ms: number): string {
  const s = Math.round(ms / 1000);
  for (const [unit, name] of [[604800, 'wk'], [86400, 'day'], [3600, 'hr'], [60, 'min']] as const) {
    if (s >= unit && s % unit === 0) return `${s / unit} ${name}`;
  }
  return `${s} sec`;
}

function policyText(t: TapView): string {
  const fresh = t.freshWithinMs ? `fresh within ${duration(t.freshWithinMs)}` : 'refreshes on every query';
  return t.waitMs ? `${fresh} · waits up to ${duration(t.waitMs)}` : fresh;
}

function KeyBox({ label, value, onDone }: { label: string; value: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ border: `1px solid ${THEME_AMBER}`, background: `${THEME_AMBER}14`, borderRadius: 6, padding: '8px 10px', marginTop: 8 }}>
      <div style={{ fontSize: 11, color: THEME_AMBER, fontWeight: 700, marginBottom: 6 }}>{label}</div>
      <code style={{ fontSize: 11, color: '#fafafa', wordBreak: 'break-all', display: 'block', marginBottom: 8 }}>{value}</code>
      <div style={{ display: 'flex', gap: 6 }}>
        <AddButton
          onClick={() => {
            navigator.clipboard?.writeText(value).then(() => setCopied(true), () => setCopied(false));
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </AddButton>
        <AddButton onClick={onDone} color="#71717a">Done</AddButton>
      </div>
    </div>
  );
}

export function TapEditor({ pond }: { pond: Pond }) {
  const taps = useLiveStore((s) => s.taps[pond.id]) ?? NO_TAPS;
  const now = useLiveStore((s) => s.now);
  const refresh = useLiveStore((s) => s.refresh);

  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const [fresh, setFresh] = useState('0');
  const [freshUnit, setFreshUnit] = useState<FreqUnit>('MINUTE');
  const [wait, setWait] = useState('');
  const [waitUnit, setWaitUnit] = useState<FreqUnit>('SECOND');
  const [tables, setTables] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // A key just minted (add or rotate), shown once with what it's for; a warning from the add, if any.
  const [shown, setShown] = useState<{ label: string; key: string; warning?: string } | null>(null);

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

  const add = () =>
    run(async () => {
      const freshN = Math.max(0, parseInt(fresh, 10) || 0);
      const waitN = parseInt(wait, 10);
      const list = tables.split(',').map((t) => t.trim()).filter(Boolean);
      const resp = await addTap(pond.id, {
        name: name.trim(),
        fresh_within_ms: freshN * UNIT_SECONDS[freshUnit] * 1000,
        wait_ms: wait && waitN > 0 ? waitN * UNIT_SECONDS[waitUnit] * 1000 : null,
        tables: list.length ? list : null,
      });
      setShown({ label: `Key for '${name.trim()}', shown once: store it now`, key: resp.key, warning: resp.warning });
      setAdding(false);
      setName('');
      setTables('');
    });

  const rotate = (t: TapView) => {
    if (!window.confirm(`Give '${t.name}' a new key? The app's current key stops working at once.`)) return;
    run(async () => {
      const resp = await rotateTap(pond.id, t.name);
      setShown({ label: `New key for '${t.name}', shown once: store it now`, key: resp.key });
    });
  };

  const remove = (t: TapView) => {
    if (!window.confirm(`Remove '${t.name}'? Its key stops working at once.`)) return;
    run(() => removeTap(pond.id, t.name));
  };

  const fld: React.CSSProperties = { ...numInput, width: '100%', boxSizing: 'border-box' };
  const row: React.CSSProperties = { display: 'flex', gap: 6, alignItems: 'center' };
  const lab: React.CSSProperties = { fontSize: 11, color: '#71717a', width: 72, flexShrink: 0 };

  return (
    <>
      <div style={{ fontSize: 10, color: '#52525b', marginBottom: 8, lineHeight: 1.5 }}>
        Access points for apps. A query through one keeps this Pond fresh while the app is in use.
      </div>
      {taps.length === 0 && !adding && !shown && <div style={{ fontSize: 12, color: '#52525b', marginBottom: 6 }}>None.</div>}
      {taps.map((t) => (
        <div key={t.name} style={{ marginBottom: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 12, color: '#e4e4e7' }}>{t.name}</span>
            <span style={{ display: 'flex', gap: 8 }}>
              <button onClick={() => rotate(t)} disabled={busy} title="New key"
                style={{ background: 'none', border: 'none', color: THEME_BRAND, cursor: 'pointer', fontSize: 11, padding: 0 }}>
                Rotate
              </button>
              <button onClick={() => remove(t)} disabled={busy} title="Remove"
                style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14, padding: 0 }}>
                ✕
              </button>
            </span>
          </div>
          <div style={{ fontSize: 11, color: '#a1a1aa' }}>{policyText(t)}</div>
          <div style={{ fontSize: 11, color: '#52525b' }}>
            {t.tables ? t.tables.join(', ') : 'all exposed tables'} ·{' '}
            {t.lastQueryAt ? `last query ${formatAge(t.lastQueryAt, now)} ago` : 'never queried'} ·{' '}
            {plural(t.queryCount, 'query', 'queries')} · {plural(t.refreshCount, 'refresh', 'refreshes')}
          </div>
        </div>
      ))}

      {shown && (
        <>
          <KeyBox label={shown.label} value={shown.key} onDone={() => setShown(null)} />
          {shown.warning && <div style={{ fontSize: 11, color: THEME_AMBER, marginTop: 6, lineHeight: 1.5 }}>{shown.warning}</div>}
        </>
      )}

      {adding ? (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
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
          <div style={row}>
            <span style={lab}>Wait</span>
            <input type="number" min="0" value={wait} onChange={(e) => setWait(e.target.value)} placeholder="no" style={numInput} />
            <select value={waitUnit} onChange={(e) => setWaitUnit(e.target.value as FreqUnit)} style={selectInput}>
              {UNITS.map((u) => <option key={u.value} value={u.value}>{u.label}</option>)}
            </select>
          </div>
          <div style={row}>
            <span style={lab}>Tables</span>
            <input value={tables} onChange={(e) => setTables(e.target.value)} placeholder="blank = all exposed" style={fld} />
          </div>
          <div style={{ fontSize: 10, color: '#52525b', lineHeight: 1.5 }}>
            Fresh within 0 refreshes on every query. Without a wait, a query is answered at once from what is published.
          </div>
          {err && <div style={{ fontSize: 11, color: THEME_DANGER, wordBreak: 'break-word' }}>{err}</div>}
          <div style={{ display: 'flex', gap: 6 }}>
            <AddButton onClick={add}>{busy ? 'Adding…' : 'Add'}</AddButton>
            <AddButton onClick={() => { setAdding(false); setErr(null); }} color="#71717a">Cancel</AddButton>
          </div>
        </div>
      ) : (
        <div style={{ marginTop: 6 }}>
          {err && <div style={{ fontSize: 11, color: THEME_DANGER, marginBottom: 6, wordBreak: 'break-word' }}>{err}</div>}
          <AddButton onClick={() => setAdding(true)}>+ Add Tap</AddButton>
        </div>
      )}
    </>
  );
}
