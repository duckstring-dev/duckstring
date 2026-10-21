'use client';

import { useState } from 'react';
import { usePlaygroundStore, refreshLabel, THEME_BRAND, THEME_DEMAND } from '@/lib/store';
import { useTourMark, useTourStore } from '@/lib/tour';
import type { FreqUnit, Pond, Refresh } from '@/lib/types';

// A Pond's Refresh, laid out like the Catchment's: Refresh Now, and Recurring Refresh opening Continuous and an
// Every box (a draft until Set or Enter applies it). The Pond's one recurring Refresh is listed by name, with ✕ to
// remove it. The playground runs in seconds, so it has no schedules.

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
  block,
}: {
  onClick: () => void;
  children: React.ReactNode;
  color: string;
  mark?: object;
  active?: boolean;
  disabled?: boolean;
  block?: boolean;
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
        whiteSpace: block ? 'normal' : 'nowrap',
        minWidth: 0,
        width: block ? '100%' : undefined,
        boxSizing: 'border-box',
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

  const continuous = current != null && current.everyMs <= 0;
  const shown = current && current.everyMs > 0 ? splitPeriod(current.everyMs) : null;

  // The Every box shows the recurring Refresh until the visitor edits it; Set (or Enter) applies the draft.
  const [draft, setDraft] = useState<string | null>(null);
  const [draftUnit, setDraftUnit] = useState<FreqUnit | null>(null);
  const value = draft ?? shown?.value ?? '';
  const unit = draftUnit ?? shown?.unit ?? 'SECOND';

  const apply = (everyMs: number) => setRefresh(pond.id, { everyMs });

  const n = parseFloat(value);
  const draftMs = value.trim() !== '' && !isNaN(n) && n >= 0 ? n * UNIT_MS[unit] : null;
  const canSet = draftMs != null && !(current && current.everyMs === draftMs);

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

  // The tour points at the Every box and the Continuous button, so it opens the panel while it does.
  const tourWants = useTourStore(
    (t) => t.phase !== 'off' && (t.outline === 'refresh-every' || t.outline === 'refresh-continuous'),
  );
  const [opened, setOpened] = useState(false);
  const open = opened || tourWants;

  return (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
        <Button onClick={() => refreshNow(pond.id)} color={THEME_DEMAND} mark={markNow} block>
          Refresh Now
        </Button>
        <Button onClick={() => setOpened((v) => !v)} color={THEME_BRAND} active={open} block>
          Recurring Refresh
        </Button>
      </div>

      {open && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <Button onClick={() => apply(0)} color={THEME_BRAND} mark={markContinuous} active={continuous} block>
            Continuous
          </Button>
          <span {...markEvery} style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
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
      )}

      {current && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 8 }}>
          <span style={{ fontSize: 12, color: '#a1a1aa' }}>{refreshLabel(current)}</span>
          <button
            onClick={remove}
            aria-label={`Remove ${refreshLabel(current)}`}
            style={{ background: 'none', border: 'none', color: '#52525b', cursor: 'pointer', fontSize: 14 }}
          >
            ✕
          </button>
        </div>
      )}
    </>
  );
}
