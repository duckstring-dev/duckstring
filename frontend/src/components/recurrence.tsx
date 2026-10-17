'use client';

import { THEME_BRAND } from '@/lib/store';
import type { FreqUnit, Weekday } from '@/lib/types';

// Recurrence fields shared by the window and Refresh editors: units, weekday chips, start times. Windows and
// scheduled Refreshes use the same grid (start + k × every, filtered by days, ended by until).

export const UNITS: { value: FreqUnit; label: string }[] = [
  { value: 'SECOND', label: 'sec' },
  { value: 'MINUTE', label: 'min' },
  { value: 'HOUR', label: 'hr' },
  { value: 'DAY', label: 'day' },
  { value: 'WEEK', label: 'wk' },
];
export const ALL_DAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

export const UNIT_SECONDS: Record<FreqUnit, number> = {
  SECOND: 1,
  MINUTE: 60,
  HOUR: 3600,
  DAY: 86400,
  WEEK: 604800,
};

export function titleCase(d: Weekday): string {
  return d[0] + d.slice(1).toLowerCase();
}

// HH:MM today (local) → ISO 8601. Empty / malformed falls back to 00:00 today.
export function todayAtISO(text: string): string {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  const d = new Date();
  d.setHours(m ? Math.min(23, +m[1]) : 0, m ? Math.min(59, +m[2]) : 0, 0, 0);
  return d.toISOString();
}

export const numInput: React.CSSProperties = {
  width: 50,
  background: '#1a1a1f',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  color: '#e4e4e7',
  padding: '3px 6px',
  fontSize: 12,
};
export const selectInput: React.CSSProperties = { ...numInput, width: 'auto' };

export function DaysPicker({ days, onToggle }: { days: Set<Weekday>; onToggle: (d: Weekday) => void }) {
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
      <span style={{ fontSize: 11, color: '#71717a', width: 56, marginTop: 4 }}>Days</span>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
        {ALL_DAYS.map((d) => (
          <button
            key={d}
            onClick={() => onToggle(d)}
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
  );
}

export function toggled(days: Set<Weekday>, d: Weekday): Set<Weekday> {
  const next = new Set(days);
  if (next.has(d)) next.delete(d);
  else next.add(d);
  return next;
}

export function OptionsToggle({ open, onClick, label = 'Options' }: { open: boolean; onClick: () => void; label?: string }) {
  return (
    <button
      onClick={onClick}
      style={{ background: 'none', border: 'none', color: THEME_BRAND, cursor: 'pointer', fontSize: 11, padding: 0 }}
    >
      {open ? `▾ ${label}` : `▸ ${label}`}
    </button>
  );
}

export function AddButton({ onClick, children, color = THEME_BRAND, block = false, active = false, disabled = false }: {
  onClick: () => void;
  children: React.ReactNode;
  color?: string;
  block?: boolean; // full width
  active?: boolean; // filled, for a toggle that's on
  disabled?: boolean;
}) {
  return (
    <button
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      style={{
        background: active ? `${color}26` : 'transparent',
        border: `1px solid ${color}`,
        color,
        borderRadius: 5,
        padding: '4px 12px',
        fontSize: 12,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.4 : 1,
        fontWeight: 600,
        letterSpacing: '0.04em',
        whiteSpace: block ? 'normal' : 'nowrap',
        minWidth: 0,
        flexShrink: 0,
        width: block ? '100%' : undefined,
        boxSizing: 'border-box',
      }}
    >
      {children}
    </button>
  );
}
