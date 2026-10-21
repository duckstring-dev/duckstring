'use client';

import { THEME_BRAND } from '@/lib/store';
import { useModeStore, type Mode } from '@/lib/mode';

// The two halves of the Playground, side by side in the sidebar's header.
const MODES: { mode: Mode; label: string }[] = [
  { mode: 'orchestration', label: 'Orchestration' },
  { mode: 'trickle', label: 'Trickle' },
];

export function ModeSwitch({ current }: { current: Mode }) {
  const setMode = useModeStore((s) => s.setMode);
  return (
    <div role="tablist" aria-label="Playground mode" style={{ display: 'inline-flex', border: '1px solid #3f3f46', borderRadius: 6, overflow: 'hidden' }}>
      {MODES.map(({ mode, label }) => {
        const active = mode === current;
        return (
          <button
            key={mode}
            role="tab"
            aria-selected={active}
            data-testid={`mode-${mode}`}
            onClick={(e) => {
              // On a phone the sheet's header toggles the sheet: the active tab is part of it, the other switches.
              if (active) return;
              e.stopPropagation();
              setMode(mode);
            }}
            style={{
              background: active ? `${THEME_BRAND}22` : 'transparent',
              color: active ? THEME_BRAND : '#a1a1aa',
              border: 'none',
              padding: '3px 10px',
              fontSize: 11,
              fontWeight: 600,
              letterSpacing: '0.04em',
              cursor: active ? 'default' : 'pointer',
              fontFamily: 'inherit',
              minHeight: 26,
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
