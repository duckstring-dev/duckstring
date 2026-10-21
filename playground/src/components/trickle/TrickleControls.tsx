'use client';

import { THEME_BRAND, THEME_SUCCESS } from '@/lib/store';
import { useTrickleStore, type Phase } from '@/lib/trickle/store';
import { MONO } from './theme';

// Play, pause and speed, over the canvas's top-left corner, with where the current run has got to.

const SPEEDS = [0.5, 1, 2];

export function phaseLabel(phase: Phase, run: number): string {
  switch (phase) {
    case 'empty':
      return 'ready';
    case 'sources':
      return `run ${run} · sources change`;
    case 'join':
      return `run ${run} · priced joins the changes`;
    case 'aggregate':
      return `run ${run} · revenue updates its totals`;
    case 'hold':
      return `run ${run} · done`;
    case 'fold':
      return `run ${run} folds into the state`;
  }
}

export function TrickleControls() {
  const paused = useTrickleStore((s) => s.paused);
  const speed = useTrickleStore((s) => s.speed);
  const phase = useTrickleStore((s) => s.phase);
  const run = useTrickleStore((s) => s.result?.run ?? 0);
  const togglePause = useTrickleStore((s) => s.togglePause);
  const setSpeed = useTrickleStore((s) => s.setSpeed);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'flex-start' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, background: '#1a1a1f', border: '1px solid #3f3f46', borderRadius: 6, padding: 4, fontFamily: MONO }}>
        <button
          data-testid="trickle-play"
          onClick={togglePause}
          title={paused ? 'Play' : 'Pause'}
          style={{ ...btn, width: 28, color: paused ? THEME_SUCCESS : '#e4e4e7', borderColor: paused ? THEME_SUCCESS : '#3f3f46' }}
        >
          {paused ? '▶' : '⏸'}
        </button>
        <span style={{ width: 1, alignSelf: 'stretch', background: '#3f3f46', margin: '0 2px' }} />
        {SPEEDS.map((sp) => (
          <button
            key={sp}
            onClick={() => setSpeed(sp)}
            style={{ ...btn, color: sp === speed ? '#0f0f14' : '#a1a1aa', background: sp === speed ? THEME_BRAND : 'transparent', borderColor: sp === speed ? THEME_BRAND : '#3f3f46' }}
          >
            {sp === 0.5 ? '1/2' : `${sp}x`}
          </button>
        ))}
      </div>
      <div data-testid="trickle-phase" style={{ fontFamily: MONO, fontSize: 11, color: '#a1a1aa', background: '#1a1a1fcc', borderRadius: 4, padding: '2px 6px' }}>
        {phaseLabel(phase, run)}
        {paused && phase !== 'empty' ? ' · paused' : ''}
      </div>
    </div>
  );
}

const btn: React.CSSProperties = {
  height: 26,
  minWidth: 32,
  padding: '0 6px',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  background: 'transparent',
  fontSize: 11,
  cursor: 'pointer',
  fontFamily: 'inherit',
};
