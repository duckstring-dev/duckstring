'use client';

import { useMemo, useState } from 'react';
import { usePlaygroundStore, THEME_BRAND } from '@/lib/store';
import { windowDelta } from '@/lib/orchestration';
import type { FreqUnit, Pond, Weekday, Window } from '@/lib/types';

// Batch-availability windows on an Inlet Pond — the playground mirror of
// `duckstring trigger window {pond} add|list|remove`. Required fields (every + name) are always
// shown; the optionals (start, duration, valid days, until) hide behind "Options" with the same
// CLI defaults (start 00:00 today, duration = every / back-to-back, all days, no expiry).

const UNITS: { value: FreqUnit; label: string }[] = [
  { value: 'SECOND', label: 'sec' },
  { value: 'MINUTE', label: 'min' },
  { value: 'HOUR', label: 'hr' },
  { value: 'DAY', label: 'day' },
  { value: 'WEEK', label: 'wk' },
];
const ALL_DAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

const UNIT_MS: Record<FreqUnit, number> = {
  SECOND: 1000,
  MINUTE: 60000,
  HOUR: 3600000,
  DAY: 86400000,
  WEEK: 604800000,
};

function hhmm(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function titleCase(d: Weekday): string {
  return d[0] + d.slice(1).toLowerCase();
}

function freqLabel(interval: number, unit: FreqUnit): string {
  if (interval === 1) {
    return { SECOND: 'Every second', MINUTE: 'Every minute', HOUR: 'Hourly', DAY: 'Daily', WEEK: 'Weekly' }[unit];
  }
  const plural = { SECOND: 'seconds', MINUTE: 'minutes', HOUR: 'hours', DAY: 'days', WEEK: 'weeks' }[unit];
  return `Every ${interval} ${plural}`;
}

// The auto-suggested name: the cadence, plus the time range when not back-to-back, plus the days.
function suggestName(w: Window): string {
  const parts = [freqLabel(w.freqInterval, w.freqUnit)];
  if (w.durationMs !== windowDelta(w)) parts.push(`${hhmm(w.startAnchor)}–${hhmm(w.startAnchor + w.durationMs)}`);
  if (w.validDays?.length && w.validDays.length < 7) parts.push(`(${w.validDays.map(titleCase).join(', ')})`);
  return parts.join(' ');
}

// HH:MM (today, local) → ms epoch. Empty / malformed falls back to 00:00 today.
function todayAt(text: string): number {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  const d = new Date();
  d.setHours(m ? Math.min(23, +m[1]) : 0, m ? Math.min(59, +m[2]) : 0, 0, 0);
  return d.getTime();
}

const numInput: React.CSSProperties = {
  width: 50,
  background: '#1a1a1f',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  color: '#e4e4e7',
  padding: '3px 6px',
  fontSize: 12,
};
const selectInput: React.CSSProperties = { ...numInput, width: 'auto' };

export function WindowEditor({ pond }: { pond: Pond }) {
  const setPondWindows = usePlaygroundStore((s) => s.setPondWindows);
  const windows = pond.windows ?? [];

  const [everyInterval, setEveryInterval] = useState('10');
  const [everyUnit, setEveryUnit] = useState<FreqUnit>('SECOND');
  const [name, setName] = useState('');
  const [nameDirty, setNameDirty] = useState(false);

  const [showOptions, setShowOptions] = useState(false);
  const [startStr, setStartStr] = useState('00:00');
  const [durInterval, setDurInterval] = useState(''); // empty ⇒ = every (back-to-back)
  const [durUnit, setDurUnit] = useState<FreqUnit>('SECOND');
  const [days, setDays] = useState<Set<Weekday>>(new Set());
  const [until, setUntil] = useState(''); // datetime-local value; empty ⇒ no expiry

  // The Window the form currently describes (used for the live name suggestion and on Add).
  const draft = useMemo<Window>(() => {
    const interval = Math.max(1, parseInt(everyInterval, 10) || 1);
    const delta = (UNIT_MS[everyUnit] ?? 0) * interval;
    const durRaw = parseInt(durInterval, 10);
    const durationMs = durInterval && durRaw > 0 ? durRaw * UNIT_MS[durUnit] : delta;
    return {
      name: '',
      startAnchor: todayAt(startStr),
      durationMs,
      freqUnit: everyUnit,
      freqInterval: interval,
      validDays: days.size ? ALL_DAYS.filter((d) => days.has(d)) : undefined,
      until: until ? new Date(until).getTime() : undefined,
    };
  }, [everyInterval, everyUnit, startStr, durInterval, durUnit, days, until]);

  const suggested = suggestName(draft);
  const effectiveName = nameDirty && name.trim() ? name.trim() : suggested;

  const toggleDay = (d: Weekday) =>
    setDays((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });

  const add = () => {
    setPondWindows(pond.id, [...windows, { ...draft, name: effectiveName }]);
    setNameDirty(false);
    setName('');
  };

  return (
    <>
      <div style={{ fontSize: 10, color: '#52525b', marginBottom: 8, lineHeight: 1.5 }}>
        When this batch source is available. Empty ⇒ live (always fresh).
      </div>

      {windows.map((w, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <span style={{ fontSize: 12, color: '#a1a1aa' }}>{w.name}</span>
          <button
            onClick={() => setPondWindows(pond.id, windows.filter((_, j) => j !== i))}
            style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14 }}
          >
            ✕
          </button>
        </div>
      ))}

      {/* Required: every + name */}
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 8 }}>
        <span style={{ fontSize: 11, color: '#71717a', width: 40 }}>Every</span>
        <input type="number" min="1" value={everyInterval} onChange={(e) => setEveryInterval(e.target.value)} style={numInput} />
        <select value={everyUnit} onChange={(e) => setEveryUnit(e.target.value as FreqUnit)} style={selectInput}>
          {UNITS.map((u) => (
            <option key={u.value} value={u.value}>{u.label}</option>
          ))}
        </select>
      </div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
        <span style={{ fontSize: 11, color: '#71717a', width: 40 }}>Name</span>
        <input
          type="text"
          value={effectiveName}
          onChange={(e) => { setName(e.target.value); setNameDirty(true); }}
          style={{ ...numInput, width: 168, flex: 1 }}
        />
      </div>

      <div style={{ marginTop: 8 }}>
        <button
          onClick={() => setShowOptions((v) => !v)}
          style={{ background: 'none', border: 'none', color: THEME_BRAND, cursor: 'pointer', fontSize: 11, padding: 0 }}
        >
          {showOptions ? '▾ Options' : '▸ Options'}
        </button>
      </div>

      {showOptions && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 56 }}>Start</span>
            <input type="text" placeholder="00:00" value={startStr} onChange={(e) => setStartStr(e.target.value)} style={{ ...numInput, width: 64 }} />
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 56 }}>Duration</span>
            <input type="number" min="1" placeholder="=every" value={durInterval} onChange={(e) => setDurInterval(e.target.value)} style={numInput} />
            <select value={durUnit} onChange={(e) => setDurUnit(e.target.value as FreqUnit)} style={selectInput}>
              {UNITS.map((u) => (
                <option key={u.value} value={u.value}>{u.label}</option>
              ))}
            </select>
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 56, marginTop: 4 }}>Days</span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
              {ALL_DAYS.map((d) => (
                <button
                  key={d}
                  onClick={() => toggleDay(d)}
                  style={{
                    background: days.has(d) ? THEME_BRAND : '#1e1e26',
                    border: '1px solid #3f3f46',
                    borderRadius: 4,
                    color: days.has(d) ? '#fff' : '#a1a1aa',
                    padding: '2px 5px',
                    fontSize: 10,
                    cursor: 'pointer',
                  }}
                >
                  {titleCase(d)}
                </button>
              ))}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 56 }}>Until</span>
            <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} style={{ ...numInput, width: 168 }} />
          </div>
          <div style={{ fontSize: 10, color: '#52525b', lineHeight: 1.5 }}>
            Defaults: start 00:00 today · duration = every (back-to-back) · all days · no expiry.
          </div>
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <button
          onClick={add}
          style={{
            background: 'transparent',
            border: `1px solid ${THEME_BRAND}`,
            color: THEME_BRAND,
            borderRadius: 5,
            padding: '4px 12px',
            fontSize: 12,
            cursor: 'pointer',
            fontWeight: 600,
            letterSpacing: '0.04em',
          }}
        >
          + Add Window
        </button>
      </div>
    </>
  );
}
