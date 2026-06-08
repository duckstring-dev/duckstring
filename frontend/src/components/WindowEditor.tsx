'use client';

import { useMemo, useState } from 'react';
import { useLiveStore } from '@/lib/store';
import type { FreqUnit, Pond, Weekday, WindowRow } from '@/lib/types';

// Batch-availability windows on an Inlet Pond — the UI for `duckstring trigger window {pond}
// add|list|remove`. Required fields (every + name) are always shown; the optionals (start, duration,
// valid days, until) hide behind "Options" with the same CLI defaults (start 00:00 today,
// duration = every / back-to-back, all days, no expiry).

const UNITS: { value: FreqUnit; label: string }[] = [
  { value: 'SECOND', label: 'sec' },
  { value: 'MINUTE', label: 'min' },
  { value: 'HOUR', label: 'hr' },
  { value: 'DAY', label: 'day' },
  { value: 'WEEK', label: 'wk' },
];
const ALL_DAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

const UNIT_SECONDS: Record<FreqUnit, number> = {
  SECOND: 1,
  MINUTE: 60,
  HOUR: 3600,
  DAY: 86400,
  WEEK: 604800,
};

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

// HH:MM today (local) → ISO 8601. Empty / malformed falls back to 00:00 today.
function todayAtISO(text: string): string {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  const d = new Date();
  d.setHours(m ? Math.min(23, +m[1]) : 0, m ? Math.min(59, +m[2]) : 0, 0, 0);
  return d.toISOString();
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

// Stable empty reference: defaulting inside the selector (`?? []`) returns a fresh array each render,
// which zustand's useSyncExternalStore reads as a changed snapshot → infinite loop.
const NO_WINDOWS: WindowRow[] = [];

export function WindowEditor({ pond }: { pond: Pond }) {
  const windows = useLiveStore((s) => s.windowsByPond[pond.id]) ?? NO_WINDOWS;
  const addWindow = useLiveStore((s) => s.addWindow);
  const removeWindow = useLiveStore((s) => s.removeWindow);

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

  const interval = Math.max(1, parseInt(everyInterval, 10) || 1);
  const durRaw = parseInt(durInterval, 10);
  const durationSeconds =
    durInterval && durRaw > 0 ? durRaw * UNIT_SECONDS[durUnit] : interval * UNIT_SECONDS[everyUnit];
  const selectedDays = ALL_DAYS.filter((d) => days.has(d));

  const suggested = useMemo(() => {
    const parts = [freqLabel(interval, everyUnit)];
    if (durInterval && durRaw > 0) parts.push(`${startStr} +${durRaw}${durUnit[0].toLowerCase()}`);
    if (selectedDays.length && selectedDays.length < 7) parts.push(`(${selectedDays.map(titleCase).join(', ')})`);
    return parts.join(' ');
  }, [interval, everyUnit, durInterval, durRaw, durUnit, startStr, selectedDays]);

  const effectiveName = nameDirty && name.trim() ? name.trim() : suggested;

  const toggleDay = (d: Weekday) =>
    setDays((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });

  const add = () => {
    addWindow(pond.id, {
      name: effectiveName,
      start_anchor: todayAtISO(startStr),
      duration_seconds: durationSeconds,
      freq_unit: everyUnit,
      freq_interval: interval,
      valid_days: selectedDays.length && selectedDays.length < 7 ? selectedDays.join(',') : null,
      until_time: until ? new Date(until).toISOString() : null,
    });
    setNameDirty(false);
    setName('');
  };

  return (
    <>
      <div style={{ fontSize: 10, color: '#52525b', marginBottom: 8, lineHeight: 1.5 }}>
        When this batch source is available. None ⇒ live (always fresh).
      </div>

      {windows.map((w) => (
        <div key={w.name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <span style={{ fontSize: 12, color: '#a1a1aa' }}>{w.name}</span>
          <button
            onClick={() => removeWindow(pond.id, w.name)}
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
          style={{ background: 'none', border: 'none', color: '#17d7c2', cursor: 'pointer', fontSize: 11, padding: 0 }}
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
                    background: days.has(d) ? '#17d7c2' : '#1e1e26',
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
            border: '1px solid #17d7c2',
            color: '#17d7c2',
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
