'use client';

import { useMemo, useState } from 'react';
import { useLiveStore, atLeast, THEME_DEMAND } from '@/lib/store';
import type { FreqUnit, Pond, RefreshView, Weekday } from '@/lib/types';
import {
  ALL_DAYS, AddButton, DaysPicker, OptionsToggle, UNITS, UNIT_SECONDS, numInput, selectInput, titleCase, todayAtISO,
  toggled,
} from './recurrence';
import { TapList } from './TapEditor';

// A Pond's Refreshes: the UI for `duckstring refresh now|add|ls|rm POND`, and below them its Taps (`refresh tap`).
// Refresh Now asks for data at least as fresh as now. A recurring Refresh repeats it: floating (every N, counted
// from the later of the last request and the last refresh of the data, so any refresh moves it; one per Pond, a
// new one replaces it), continuous, or scheduled (a start: fires on that grid whatever else refreshes the Pond).
// The list shows names only; a click (or hover) shows what each is and when it next asks.

type Unit = FreqUnit;
const NO_REFRESHES: RefreshView[] = [];

function periodName(seconds: number): string {
  for (const [unit, one, many] of [[604800, 'Weekly', 'weeks'], [86400, 'Daily', 'days'], [3600, 'Hourly', 'h'],
                                   [60, null, 'min'], [1, null, 's']] as const) {
    if (seconds % unit === 0) {
      const n = seconds / unit;
      return n === 1 && one ? one : `Every ${n} ${many}`;
    }
  }
  return `Every ${seconds} s`;
}

// The name a Refresh is given unless one is typed (as the Catchment names them, in local time here).
function suggestName(seconds: number, continuous: boolean, start: string, days: Weekday[]): string {
  if (continuous) return 'Continuous';
  if (!start.trim()) return periodName(seconds);
  const base = periodName(seconds);
  const weekdays = days.length === 5 && ['MON', 'TUE', 'WED', 'THU', 'FRI'].every((d) => days.includes(d as Weekday));
  if (weekdays && base === 'Daily') return `Weekdays at ${start.trim()}`;
  return `${base} at ${start.trim()}${days.length && days.length < 7 ? ` (${days.map(titleCase).join(', ')})` : ''}`;
}

// What a recurring Refresh is, for its detail line.
function describe(r: RefreshView): string {
  if (r.continuous) return 'Continuous';
  const period = periodName(Math.round(r.everyMs / 1000));
  if (!r.start) return period;
  const d = new Date(r.start);
  const at = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const days = r.days ? ` (${r.days.split(',').map((x) => titleCase(x as Weekday)).join(', ')})` : '';
  const until = r.until ? `, until ${new Date(r.until).toLocaleString()}` : '';
  return `${period} from ${at}${days}${until}`;
}

function nextText(r: RefreshView, now: number): string {
  if (r.continuous) return 'as often as it can';
  if (r.nextDue == null) return 'ended';
  const secs = Math.round((r.nextDue - now) / 1000);
  if (secs <= 0) return 'due now';
  if (secs < 60) return `next in ${secs}s`;
  if (secs < 3600) return `next in ${Math.round(secs / 60)}m`;
  const d = new Date(r.nextDue);
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return secs < 86400 ? `next ${hhmm}` : `next ${hhmm} ${d.toLocaleDateString(undefined, { weekday: 'short' })}`;
}

export function RefreshEditor({ pond, canDemand }: { pond: Pond; canDemand: boolean }) {
  const refreshes = useLiveStore((s) => s.refreshes[pond.id]) ?? NO_REFRESHES;
  const now = useLiveStore((s) => s.now);
  const refreshNow = useLiveStore((s) => s.refreshNow);
  const addRefresh = useLiveStore((s) => s.addRefresh);
  const removeRefresh = useLiveStore((s) => s.removeRefresh);
  const canManage = useLiveStore((s) => atLeast(s.accessLevel, 'full'));

  const [adding, setAdding] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null); // the Refresh whose detail line is open
  const [every, setEvery] = useState('30');
  const [unit, setUnit] = useState<Unit>('MINUTE');
  const [name, setName] = useState('');
  const [nameDirty, setNameDirty] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const [start, setStart] = useState('');
  const [days, setDays] = useState<Set<Weekday>>(new Set());
  const [until, setUntil] = useState('');

  const seconds = Math.max(1, parseFloat(every) || 1) * UNIT_SECONDS[unit];
  const selectedDays = ALL_DAYS.filter((d) => days.has(d));
  const daysKey = selectedDays.join(',');
  const suggested = useMemo(
    () => suggestName(seconds, false, start, daysKey ? (daysKey.split(',') as Weekday[]) : []),
    [seconds, start, daysKey],
  );
  const effectiveName = nameDirty && name.trim() ? name.trim() : suggested;
  const scheduled = start.trim() !== '';

  const done = () => {
    setNameDirty(false);
    setName('');
    setAdding(false);
  };
  const addContinuous = () => {
    addRefresh(pond.id, { every_seconds: 0, start: null, days: null, until: null, name: 'Continuous' });
    done();
  };
  const add = () => {
    addRefresh(pond.id, {
      every_seconds: seconds,
      start: scheduled ? todayAtISO(start) : null,
      days: scheduled && selectedDays.length && selectedDays.length < 7 ? selectedDays.join(',') : null,
      until: scheduled && until ? new Date(until).toISOString() : null,
      name: effectiveName,
    });
    done();
  };

  return (
    <>
      {canDemand && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 8 }}>
          <AddButton block onClick={() => refreshNow(pond.id)} color={THEME_DEMAND}>Refresh Now</AddButton>
          <AddButton block active={adding} onClick={() => setAdding((v) => !v)}>Recurring Refresh</AddButton>
        </div>
      )}

      {canDemand && adding && (
        <div style={{ marginBottom: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <AddButton block onClick={addContinuous}>Continuous</AddButton>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 2 }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 40 }}>Every</span>
            <input type="number" min="1" value={every} onChange={(e) => setEvery(e.target.value)} style={numInput} />
            <select value={unit} onChange={(e) => setUnit(e.target.value as Unit)} style={selectInput}>
              {UNITS.map((u) => (
                <option key={u.value} value={u.value}>{u.label}</option>
              ))}
            </select>
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span style={{ fontSize: 11, color: '#71717a', width: 40 }}>Name</span>
            <input
              type="text"
              value={effectiveName}
              onChange={(e) => { setName(e.target.value); setNameDirty(true); }}
              style={{ ...numInput, width: 168, flex: 1 }}
            />
          </div>
          <div>
            <OptionsToggle open={showOptions} onClick={() => setShowOptions((v) => !v)} />
          </div>
          {showOptions && (
            <>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <span style={{ fontSize: 11, color: '#71717a', width: 56 }}>Start</span>
                <input type="text" placeholder="HH:MM" value={start} onChange={(e) => setStart(e.target.value)} style={{ ...numInput, width: 64 }} />
              </div>
              {scheduled && <DaysPicker days={days} onToggle={(d) => setDays((prev) => toggled(prev, d))} />}
              {scheduled && (
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <span style={{ fontSize: 11, color: '#71717a', width: 56 }}>Until</span>
                  <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} style={{ ...numInput, width: 168 }} />
                </div>
              )}
            </>
          )}
          <div style={{ marginTop: 4 }}>
            <AddButton onClick={add}>+ Add Refresh</AddButton>
          </div>
        </div>
      )}

      {refreshes.map((r) => {
        const detail = `${describe(r)} · ${nextText(r, now)}`;
        return (
          <div key={r.name} style={{ marginBottom: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6 }}>
              <span
                role="button"
                title={detail}
                onClick={() => setExpanded((v) => (v === r.name ? null : r.name))}
                style={{ fontSize: 12, color: '#a1a1aa', cursor: 'pointer', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              >
                {r.name}
              </span>
              {canDemand && (
                <button
                  onClick={() => removeRefresh(pond.id, r.name)}
                  aria-label={`Remove ${r.name}`}
                  style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14, flexShrink: 0 }}
                >
                  ✕
                </button>
              )}
            </div>
            {expanded === r.name && <div style={{ fontSize: 10, color: '#71717a', marginTop: 1 }}>{detail}</div>}
          </div>
        );
      })}

      {canManage && <TapList pond={pond} />}
    </>
  );
}
