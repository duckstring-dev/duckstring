'use client';

import { useMemo, useState } from 'react';
import { usePlaygroundStore, refreshLabel, THEME_BRAND, THEME_DEMAND } from '@/lib/store';
import { refreshNextDue, type OrchestrState } from '@/lib/orchestration';
import { useTourMark } from '@/lib/tour';
import type { FreqUnit, Pond, Refresh, Weekday } from '@/lib/types';

// A Pond's Refreshes: refresh now, the standing Refreshes with when each next sends, and a form to add one. The
// form follows the WindowEditor's shape: every + name always shown, the rest behind "Options". A floating Refresh
// (no start) replaces the Pond's floating one; one with a start fires on its grid and adds up. Continuous (a
// floating Refresh every 0 s) has its own button.

const UNITS: { value: FreqUnit; label: string }[] = [
  { value: 'SECOND', label: 'sec' },
  { value: 'MINUTE', label: 'min' },
  { value: 'HOUR', label: 'hr' },
  { value: 'DAY', label: 'day' },
  { value: 'WEEK', label: 'wk' },
];
const UNIT_MS: Record<FreqUnit, number> = { SECOND: 1000, MINUTE: 60000, HOUR: 3600000, DAY: 86400000, WEEK: 604800000 };
const ALL_DAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
const NO_REFRESHES: Refresh[] = [];

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

function titleCase(d: Weekday): string {
  return d[0] + d.slice(1).toLowerCase();
}

// HH:MM or HH:MM:SS today (local) as ms epoch; null if malformed.
function todayAt(text: string): number | null {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(text.trim());
  if (!m) return null;
  const d = new Date();
  d.setHours(Math.min(23, +m[1]), Math.min(59, +m[2]), Math.min(59, +(m[3] ?? 0)), 0);
  return d.getTime();
}

function dueText(due: number | null, now: number, continuous: boolean): string {
  if (continuous) return 'continuous';
  if (due == null) return 'ended';
  const secs = (due - now) / 1000;
  if (secs <= 0.05) return 'due now';
  if (secs < 3600) return `next in ${secs < 10 ? secs.toFixed(1) : Math.round(secs)} s`;
  const d = new Date(due);
  return `next ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function Button({
  onClick,
  children,
  color,
  mark,
  disabled,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color: string;
  mark?: object;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      {...mark}
      style={{
        opacity: disabled ? 0.4 : 1,
        background: 'transparent',
        border: `1px solid ${color}`,
        color,
        borderRadius: 5,
        padding: '4px 12px',
        fontSize: 12,
        cursor: disabled ? 'default' : 'pointer',
        fontWeight: 600,
        letterSpacing: '0.04em',
      }}
    >
      {children}
    </button>
  );
}

export function RefreshEditor({ pond }: { pond: Pond }) {
  const refreshes = usePlaygroundStore((s) => s.refreshes[pond.id]) ?? NO_REFRESHES;
  const now = usePlaygroundStore((s) => s.now);
  const refreshNow = usePlaygroundStore((s) => s.refreshNow);
  const addRefresh = usePlaygroundStore((s) => s.addRefresh);
  const removeRefresh = usePlaygroundStore((s) => s.removeRefresh);
  const markNow = useTourMark('refresh-now');
  const markContinuous = useTourMark('refresh-continuous');
  const markList = useTourMark('refresh-list');
  const markAdd = useTourMark('refresh-add');

  const [every, setEvery] = useState('10');
  const [unit, setUnit] = useState<FreqUnit>('SECOND');
  const [name, setName] = useState('');
  const [nameDirty, setNameDirty] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const [start, setStart] = useState('');
  const [days, setDays] = useState<Set<Weekday>>(new Set());
  const [until, setUntil] = useState('');

  const everyMs = Math.max(0, parseFloat(every) || 0) * UNIT_MS[unit];
  const startAnchor = start.trim() ? (todayAt(start) ?? undefined) : undefined;
  const isContinuous = (r: Refresh) => r.startAnchor == null && r.everyMs <= 0;
  const continuousOn = refreshes.some(isContinuous);
  const selectedDays = ALL_DAYS.filter((d) => days.has(d));
  const draft: Omit<Refresh, 'name'> = {
    everyMs,
    ...(startAnchor != null && { startAnchor }),
    ...(selectedDays.length && selectedDays.length < 7 && { validDays: selectedDays }),
    ...(until && { until: new Date(until).getTime() }),
  };
  const daysKey = selectedDays.join(',');
  const suggested = useMemo(() => {
    const base = refreshLabel({ everyMs, ...(startAnchor != null && { startAnchor }) });
    const dayList = daysKey ? daysKey.split(',') as Weekday[] : [];
    return dayList.length && dayList.length < 7 ? `${base} (${dayList.map(titleCase).join(', ')})` : base;
  }, [everyMs, startAnchor, daysKey]);
  const effectiveName = nameDirty && name.trim() ? name.trim() : suggested;

  const add = () => {
    if (startAnchor != null && everyMs <= 0) return; // a start needs a period
    addRefresh(pond.id, { ...draft, name: effectiveName });
    setNameDirty(false);
    setName('');
  };

  const toggleDay = (d: Weekday) =>
    setDays((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });

  const state = usePlaygroundStore.getState() as unknown as OrchestrState;

  return (
    <>
      <div style={{ fontSize: 10, color: '#52525b', lineHeight: 1.5, marginBottom: 6 }}>
        Ask for this Pond&apos;s data as of now, or keep asking.
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        <Button onClick={() => refreshNow(pond.id)} color={THEME_DEMAND} mark={markNow}>
          Refresh now
        </Button>
        <Button onClick={() => addRefresh(pond.id, { everyMs: 0 })} color={THEME_BRAND} mark={markContinuous} disabled={continuousOn}>
          Continuous
        </Button>
      </div>

      <div {...markList}>
        {refreshes.map((r) => (
          <div key={r.name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6, marginBottom: 4 }}>
            <span style={{ fontSize: 12, color: '#a1a1aa', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.name}</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
              <span style={{ fontSize: 10, color: '#71717a' }}>
                {dueText(refreshNextDue(state, pond.id, r, now), now, isContinuous(r))}
              </span>
              <button
                onClick={() => removeRefresh(pond.id, r.name)}
                aria-label={`Remove ${r.name}`}
                style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14 }}
              >
                ✕
              </button>
            </span>
          </div>
        ))}
      </div>

      <div {...markAdd} style={{ marginTop: 8 }}>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: '#71717a', width: 40 }}>Every</span>
          <input type="number" min="0" value={every} onChange={(e) => setEvery(e.target.value)} style={numInput} />
          <select value={unit} onChange={(e) => setUnit(e.target.value as FreqUnit)} style={selectInput}>
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
            onChange={(e) => {
              setName(e.target.value);
              setNameDirty(true);
            }}
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
              <input type="text" placeholder="HH:MM" value={start} onChange={(e) => setStart(e.target.value)} style={{ ...numInput, width: 72 }} />
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
              No start: counts from the last request or refresh. A start: fires on that grid, whatever else refreshes the Pond.
            </div>
          </div>
        )}

        <div style={{ marginTop: 10 }}>
          <Button onClick={add} color={THEME_BRAND}>
            + Add Refresh
          </Button>
        </div>
      </div>
    </>
  );
}
