'use client';

import { usePlaygroundStore, THEME_BRAND, THEME_SUCCESS, THEME_PULL } from '@/lib/store';

// Selectable run speeds (multipliers of real time).
const SPEEDS = [0.1, 0.5, 1, 2, 10];
function speedLabel(s: number): string {
  if (s === 0.1) return '1/10';
  if (s === 0.5) return '1/2';
  return `${s}x`;
}

export function SimControls() {
  const paused = usePlaygroundStore((s) => s.paused);
  const speed = usePlaygroundStore((s) => s.speed);
  const togglePause = usePlaygroundStore((s) => s.togglePause);
  const setSpeed = usePlaygroundStore((s) => s.setSpeed);
  const throttle = usePlaygroundStore((s) => s.pulseThrottle);
  const setThrottle = usePlaygroundStore((s) => s.setPulseThrottle);

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 4,
        background: '#1a1a1f',
        border: '1px solid #3f3f46',
        borderRadius: 6,
        padding: 4,
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
      }}
    >
      <button
        onClick={togglePause}
        title={paused ? 'Play' : 'Pause'}
        style={{
          ...btnBase,
          width: 28,
          color: paused ? THEME_SUCCESS : '#e4e4e7',
          borderColor: paused ? THEME_SUCCESS : '#3f3f46',
        }}
      >
        {paused ? '▶' : '⏸'}
      </button>
      <span style={{ width: 1, alignSelf: 'stretch', background: '#3f3f46', margin: '0 2px' }} />
      {SPEEDS.map((sp) => {
        const active = sp === speed;
        return (
          <button
            key={sp}
            onClick={() => setSpeed(sp)}
            style={{
              ...btnBase,
              color: active ? '#0f0f14' : '#a1a1aa',
              background: active ? THEME_BRAND : 'transparent',
              borderColor: active ? THEME_BRAND : '#3f3f46',
            }}
          >
            {speedLabel(sp)}
          </button>
        );
      })}
      <span style={{ width: 1, alignSelf: 'stretch', background: '#3f3f46', margin: '0 2px' }} />
      <button
        onClick={() => setThrottle(!throttle)}
        title="Pulse throttle (experimental): a Ripple that sees two demands in one run throttles its Pond and everything downstream; a throttled Outlet sends one queued Pulse each time the slow Ripple turns over"
        data-testid="throttle-toggle"
        style={{
          ...btnBase,
          color: throttle ? '#0f0f14' : '#a1a1aa',
          background: throttle ? THEME_PULL : 'transparent',
          borderColor: throttle ? THEME_PULL : '#3f3f46',
        }}
      >
        Throttle {throttle ? 'on' : 'off'}
      </button>
    </div>
  );
}

const btnBase: React.CSSProperties = {
  border: '1px solid #3f3f46',
  borderRadius: 4,
  padding: '3px 8px',
  fontSize: 11,
  fontWeight: 600,
  cursor: 'pointer',
  letterSpacing: '0.03em',
  lineHeight: 1,
};
