'use client';

import { useState } from 'react';
import { usePlaygroundStore, refreshLabel, THEME_BRAND, THEME_DEMAND } from '@/lib/store';
import { refreshNextDue, type OrchestrState } from '@/lib/orchestration';
import { useTourMark } from '@/lib/tour';
import type { FreqUnit, Pond, Refresh, Weekday } from '@/lib/types';

// A Pond's Refresh: refresh now, and its one standing Refresh. The Every box shows the standing Refresh and edits
// it in place (applied on Enter or on leaving the box); Continuous toggles a Refresh every 0 s; the options (a
// start that pins it to a schedule, days, an end) apply to the same Refresh.

const UNITS: { value: FreqUnit; label: string }[] = [
  { value: 'SECOND', label: 'sec' },
  { value: 'MINUTE', label: 'min' },
  { value: 'HOUR', label: 'hr' },
  { value: 'DAY', label: 'day' },
  { value: 'WEEK', label: 'wk' },
];
const UNIT_MS: Record<FreqUnit, number> = { SECOND: 1000, MINUTE: 60000, HOUR: 3600000, DAY: 86400000, WEEK: 604800000 };
const ALL_DAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

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

function hhmm(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// A period in the largest unit that divides it evenly (seconds otherwise).
function splitPeriod(ms: number): { value: string; unit: FreqUnit } {
  for (const unit of ['WEEK', 'DAY', 'HOUR', 'MINUTE'] as FreqUnit[]) {
    if (ms % UNIT_MS[unit] === 0) return { value: String(ms / UNIT_MS[unit]), unit };
  }
  return { value: String(ms / 1000), unit: 'SECOND' };
}

function dueText(due: number | null, now: number, continuous: boolean): string {
  if (continuous) return 'as often as it can';
  if (due == null) return 'ended';
  const secs = (due - now) / 1000;
  if (secs <= 0.05) return 'due now';
  if (secs < 3600) return `next in ${secs < 10 ? secs.toFixed(1) : Math.round(secs)} s`;
  return `next ${hhmm(due)}`;
}

function Button({
  onClick,
  children,
  color,
  mark,
  active,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color: string;
  mark?: object;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      {...mark}
      style={{
        background: active ? color : 'transparent',
        border: `1px solid ${color}`,
        color: active ? '#0b0b0e' : color,
        borderRadius: 5,
        padding: '4px 12px',
        fontSize: 12,
        cursor: 'pointer',
        fontWeight: 600,
        letterSpacing: '0.04em',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </button>
  );
}

export function RefreshEditor({ pond }: { pond: Pond }) {
  const current: Refresh | undefined = usePlaygroundStore((s) => s.refreshes[pond.id]?.[0]);
  const now = usePlaygroundStore((s) => s.now);
  const refreshNow = usePlaygroundStore((s) => s.refreshNow);
  const setRefresh = usePlaygroundStore((s) => s.setRefresh);
  const markNow = useTourMark('refresh-now');
  const markEvery = useTourMark('refresh-every');
  const markContinuous = useTourMark('refresh-continuous');

  const continuous = current != null && current.startAnchor == null && current.everyMs <= 0;
  const shown = current && current.everyMs > 0 ? splitPeriod(current.everyMs) : null;

  // The Every box shows the standing Refresh until the visitor types; a draft is applied on Enter or blur.
  const [draft, setDraft] = useState<string | null>(null);
  const [draftUnit, setDraftUnit] = useState<FreqUnit | null>(null);
  const value = draft ?? shown?.value ?? '';
  const unit = draftUnit ?? shown?.unit ?? 'SECOND';

  const [showOptions, setShowOptions] = useState(current?.startAnchor != null || !!current?.validDays || current?.until != null);
  const [start, setStart] = useState(current?.startAnchor != null ? hhmm(current.startAnchor) : '');
  const [days, setDays] = useState<Set<Weekday>>(new Set(current?.validDays ?? []));
  const [until, setUntil] = useState('');

  // Apply a period (ms) with the options; a start needs a period, so continuous drops it.
  const apply = (everyMs: number, opts = { start, days, until }) => {
    const anchor = everyMs > 0 && opts.start.trim() ? (todayAt(opts.start) ?? undefined) : undefined;
    const selected = ALL_DAYS.filter((d) => opts.days.has(d));
    setRefresh(pond.id, {
      everyMs,
      ...(anchor != null && { startAnchor: anchor }),
      ...(selected.length && selected.length < 7 && { validDays: selected }),
      ...(opts.until && { until: new Date(opts.until).getTime() }),
    });
  };

  const commit = (text = value, u = unit) => {
    setDraft(null);
    setDraftUnit(null);
    const n = parseFloat(text);
    if (text.trim() === '' || isNaN(n) || n < 0) return; // nothing to apply: the box shows the Refresh again
    apply(n * UNIT_MS[u]);
  };

  // Options edit the standing Refresh in place (when it has a period).
  const changeOptions = (opts: { start: string; days: Set<Weekday>; until: string }) => {
    if (current && current.everyMs > 0) apply(current.everyMs, opts);
  };

  const toggleDay = (d: Weekday) => {
    const next = new Set(days);
    if (next.has(d)) next.delete(d);
    else next.add(d);
    setDays(next);
    changeOptions({ start, days: next, until });
  };

  const state = usePlaygroundStore.getState() as unknown as OrchestrState;

  return (
    <>
      <div style={{ fontSize: 10, color: '#52525b', lineHeight: 1.5, marginBottom: 6 }}>
        Ask for this Pond&apos;s data as of now, or keep asking.
      </div>
      <div style={{ marginBottom: 10 }}>
        <Button onClick={() => refreshNow(pond.id)} color={THEME_DEMAND} mark={markNow}>
          Refresh now
        </Button>
      </div>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
        <span {...markEvery} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: '#71717a' }}>Every</span>
          <input
            type="number"
            min="0"
            inputMode="decimal"
            aria-label="Refresh every"
            placeholder={continuous ? '–' : ''}
            value={value}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => draft != null && commit()}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit();
              if (e.key === 'Escape') {
                setDraft(null);
                setDraftUnit(null);
              }
            }}
            style={numInput}
          />
          <select
            value={unit}
            aria-label="Unit"
            onChange={(e) => {
              const u = e.target.value as FreqUnit;
              if (value.trim() !== '') commit(value, u);
              else setDraftUnit(u);
            }}
            style={selectInput}
          >
            {UNITS.map((u) => (
              <option key={u.value} value={u.value}>{u.label}</option>
            ))}
          </select>
        </span>
        <Button
          onClick={() => (continuous ? setRefresh(pond.id, null) : apply(0))}
          color={THEME_BRAND}
          mark={markContinuous}
          active={continuous}
        >
          Continuous
        </Button>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6, marginTop: 8, minHeight: 20 }}>
        {current ? (
          <>
            <span style={{ fontSize: 12, color: '#a1a1aa', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {refreshLabel(current)}
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
              <span style={{ fontSize: 10, color: '#71717a' }}>{dueText(refreshNextDue(state, pond.id, current, now), now, continuous)}</span>
              <button
                onClick={() => setRefresh(pond.id, null)}
                aria-label="Remove the Refresh"
                style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14 }}
              >
                ✕
              </button>
            </span>
          </>
        ) : (
          <span style={{ fontSize: 11, color: '#52525b' }}>No standing Refresh.</span>
        )}
      </div>

      <div style={{ marginTop: 6 }}>
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
            <input
              type="text"
              placeholder="HH:MM"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              onBlur={() => changeOptions({ start, days, until })}
              onKeyDown={(e) => e.key === 'Enter' && changeOptions({ start, days, until })}
              style={{ ...numInput, width: 72 }}
            />
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
            <input
              type="datetime-local"
              value={until}
              onChange={(e) => {
                setUntil(e.target.value);
                changeOptions({ start, days, until: e.target.value });
              }}
              style={{ ...numInput, width: 168 }}
            />
          </div>
          <div style={{ fontSize: 10, color: '#52525b', lineHeight: 1.5 }}>
            No start: counts from the last request or refresh. A start: fires on that grid, whatever else refreshes the Pond.
          </div>
        </div>
      )}
    </>
  );
}
