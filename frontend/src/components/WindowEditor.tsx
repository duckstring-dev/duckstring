'use client';

import { useMemo, useState } from 'react';
import { useLiveStore } from '@/lib/store';
import type { FreqUnit, Pond, Weekday, WindowRow } from '@/lib/types';
import {
  ALL_DAYS, AddButton, DaysPicker, OptionsToggle, UNITS, UNIT_SECONDS, numInput, selectInput, titleCase, todayAtISO,
  toggled,
} from './recurrence';

// Batch-availability windows on an Inlet Pond — the UI for `duckstring window add|ls|rm {pond}`. Required fields (every + name) are always shown; the optionals (start, duration,
// valid days, until) hide behind "Options" with the same CLI defaults (start 00:00 today,
// duration = every / back-to-back, all days, no expiry).

function freqLabel(interval: number, unit: FreqUnit): string {
  if (interval === 1) {
    return { SECOND: 'Every second', MINUTE: 'Every minute', HOUR: 'Hourly', DAY: 'Daily', WEEK: 'Weekly' }[unit];
  }
  const plural = { SECOND: 'seconds', MINUTE: 'minutes', HOUR: 'hours', DAY: 'days', WEEK: 'weeks' }[unit];
  return `Every ${interval} ${plural}`;
}

// Stable empty reference: defaulting inside the selector (`?? []`) returns a fresh array each render,
// which zustand's useSyncExternalStore reads as a changed snapshot → infinite loop.
const NO_WINDOWS: WindowRow[] = [];

// `readOnly` (below full access) shows the window list but no add/remove controls — viewing an Inlet's
// batch schedule is `read`-gated server-side; editing it is full only.
export function WindowEditor({ pond, readOnly = false }: { pond: Pond; readOnly?: boolean }) {
  const windows = useLiveStore((s) => s.windowsByPond[pond.id]) ?? NO_WINDOWS;
  const addWindow = useLiveStore((s) => s.addWindow);
  const removeWindow = useLiveStore((s) => s.removeWindow);

  const [everyInterval, setEveryInterval] = useState('10');
  const [everyUnit, setEveryUnit] = useState<FreqUnit>('SECOND');
  const [name, setName] = useState('');
  const [nameDirty, setNameDirty] = useState(false);

  const [adding, setAdding] = useState(false);
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

  const toggleDay = (d: Weekday) => setDays((prev) => toggled(prev, d));

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
    setAdding(false);
  };

  return (
    <>
      {windows.map((w) => (
        <div key={w.name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <span style={{ fontSize: 12, color: '#a1a1aa' }}>{w.name}</span>
          {!readOnly && (
            <button
              onClick={() => removeWindow(pond.id, w.name)}
              style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14 }}
            >
              ✕
            </button>
          )}
        </div>
      ))}

      {readOnly && windows.length === 0 && (
        <div style={{ fontSize: 12, color: '#52525b' }}>None.</div>
      )}

      {!readOnly && (
        <div style={{ marginTop: 8 }}>
          <AddButton block active={adding} onClick={() => setAdding((v) => !v)}>Add Window</AddButton>
        </div>
      )}

      {!readOnly && adding && (<>
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
        <OptionsToggle open={showOptions} onClick={() => setShowOptions((v) => !v)} />
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
          <DaysPicker days={days} onToggle={toggleDay} />
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 56 }}>Until</span>
            <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} style={{ ...numInput, width: 168 }} />
          </div>
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <AddButton onClick={add}>Add</AddButton>
      </div>
      </>)}
    </>
  );
}
