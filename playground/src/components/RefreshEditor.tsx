'use client';

import { useState } from 'react';
import { usePlaygroundStore, THEME_BRAND, THEME_DANGER, THEME_DEMAND } from '@/lib/store';
import { useTourMark } from '@/lib/tour';
import type { FreqUnit, Pond, Refresh } from '@/lib/types';

// A Pond's Refresh: refresh now, and its one standing Refresh. The Every box shows the standing Refresh; an edit is
// a draft until Set (or Enter) applies it. Continuous sets a Refresh every 0 s, and Remove drops the Refresh.

const UNITS: { value: FreqUnit; label: string }[] = [
  { value: 'SECOND', label: 'sec' },
  { value: 'MINUTE', label: 'min' },
  { value: 'HOUR', label: 'hr' },
  { value: 'DAY', label: 'day' },
  { value: 'WEEK', label: 'wk' },
];
const UNIT_MS: Record<FreqUnit, number> = { SECOND: 1000, MINUTE: 60000, HOUR: 3600000, DAY: 86400000, WEEK: 604800000 };

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

// A period in the largest unit that divides it evenly (seconds otherwise).
function splitPeriod(ms: number): { value: string; unit: FreqUnit } {
  for (const unit of ['WEEK', 'DAY', 'HOUR', 'MINUTE'] as FreqUnit[]) {
    if (ms % UNIT_MS[unit] === 0) return { value: String(ms / UNIT_MS[unit]), unit };
  }
  return { value: String(ms / 1000), unit: 'SECOND' };
}

function Button({
  onClick,
  children,
  color,
  mark,
  active,
  disabled,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color: string;
  mark?: object;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      disabled={disabled}
      {...mark}
      style={{
        opacity: disabled ? 0.35 : 1,
        background: active ? color : 'transparent',
        border: `1px solid ${color}`,
        color: active ? '#0b0b0e' : color,
        borderRadius: 5,
        padding: '4px 12px',
        fontSize: 12,
        cursor: disabled ? 'default' : 'pointer',
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
  const refreshNow = usePlaygroundStore((s) => s.refreshNow);
  const setRefresh = usePlaygroundStore((s) => s.setRefresh);
  const markNow = useTourMark('refresh-now');
  const markEvery = useTourMark('refresh-every');
  const markContinuous = useTourMark('refresh-continuous');

  const continuous = current != null && current.startAnchor == null && current.everyMs <= 0;
  const shown = current && current.everyMs > 0 ? splitPeriod(current.everyMs) : null;

  // The Every box shows the standing Refresh until the visitor edits it; Set (or Enter) applies the draft.
  const [draft, setDraft] = useState<string | null>(null);
  const [draftUnit, setDraftUnit] = useState<FreqUnit | null>(null);
  const value = draft ?? shown?.value ?? '';
  const unit = draftUnit ?? shown?.unit ?? 'SECOND';

  const apply = (everyMs: number) => setRefresh(pond.id, { everyMs });

  const n = parseFloat(value);
  const draftMs = value.trim() !== '' && !isNaN(n) && n >= 0 ? n * UNIT_MS[unit] : null;
  const canSet = draftMs != null && !(current && current.startAnchor == null && current.everyMs === draftMs);

  const clearDraft = () => {
    setDraft(null);
    setDraftUnit(null);
  };
  const commit = () => {
    if (!canSet) return;
    clearDraft();
    apply(draftMs);
  };
  const remove = () => {
    clearDraft();
    setRefresh(pond.id, null);
  };

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
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit();
              if (e.key === 'Escape') clearDraft();
            }}
            style={numInput}
          />
          <select
            value={unit}
            aria-label="Unit"
            onChange={(e) => {
              setDraft(value);
              setDraftUnit(e.target.value as FreqUnit);
            }}
            style={selectInput}
          >
            {UNITS.map((u) => (
              <option key={u.value} value={u.value}>{u.label}</option>
            ))}
          </select>
          <Button onClick={commit} color={THEME_BRAND} disabled={!canSet}>
            Set
          </Button>
        </span>
      </div>
      <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
        <Button onClick={() => apply(0)} color={THEME_BRAND} mark={markContinuous} active={continuous}>
          Continuous
        </Button>
        <Button onClick={remove} color={THEME_DANGER} disabled={!current}>
          Remove
        </Button>
      </div>
    </>
  );
}
