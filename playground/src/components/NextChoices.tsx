'use client';

import { track } from '@vercel/analytics';
import { THEME_BRAND } from '@/lib/store';
import { useModeStore, type Mode } from '@/lib/mode';

// The ways on from a tour's last card, weighted by how strongly each is suggested: `primary` (the main route, a
// big cyan button), `secondary` (a full-width outlined button), `muted` (a small button in a row) and `quiet`
// (a plain text link). Every choice is counted in Vercel Analytics under `event`, since this is where most
// visitors leave the Playground.

export interface Choice {
  id: string;
  label: string;
  detail?: string;
  href?: string; // a page, in a new tab
  mode?: Mode; // the other half of the Playground
  close?: boolean; // just end the tour
  tone?: 'primary' | 'secondary' | 'muted' | 'quiet';
}

export function NextChoices({ choices, event, onClose, testId }: {
  choices: Choice[];
  event: string;
  onClose: () => void; // ends the tour; switching mode ends it first
  testId: (c: Choice) => string;
}) {
  const go = (c: Choice) => {
    track(event, { choice: c.id });
    if (c.href) return; // the link opens the page
    onClose();
    if (c.mode) useModeStore.getState().setMode(c.mode);
  };
  const tone = (c: Choice) => c.tone ?? 'secondary';
  const big = choices.filter((c) => tone(c) === 'primary' || tone(c) === 'secondary');
  const muted = choices.filter((c) => tone(c) === 'muted');
  const quiet = choices.filter((c) => tone(c) === 'quiet');
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {big.map((c) => (
        <Item key={c.id} choice={c} onGo={() => go(c)} testId={testId(c)} style={bigStyle(tone(c) === 'primary')}>
          <div style={{ fontSize: 13.5, fontWeight: 700 }}>
            {c.label}
            {arrow(c)}
          </div>
          {c.detail && (
            <div style={{ fontSize: 11.5, marginTop: 2, fontWeight: 400, color: tone(c) === 'primary' ? '#0f0f14cc' : '#a1a1aa' }}>{c.detail}</div>
          )}
        </Item>
      ))}
      {muted.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {muted.map((c) => (
            <Item key={c.id} choice={c} onGo={() => go(c)} testId={testId(c)} style={mutedStyle}>
              {c.label}
              {arrow(c)}
            </Item>
          ))}
        </div>
      )}
      {quiet.map((c) => (
        <Item key={c.id} choice={c} onGo={() => go(c)} testId={testId(c)} style={quietStyle}>
          {c.label}
        </Item>
      ))}
    </div>
  );
}

const arrow = (c: Choice) => (c.href ? ' ↗' : c.mode ? ' →' : '');

function Item({ choice, onGo, testId, style, children }: {
  choice: Choice; onGo: () => void; testId: string; style: React.CSSProperties; children: React.ReactNode;
}) {
  return choice.href ? (
    <a href={choice.href} target="_blank" rel="noopener noreferrer" onClick={onGo} style={style} data-testid={testId}>
      {children}
    </a>
  ) : (
    <button onClick={onGo} style={style} data-testid={testId}>
      {children}
    </button>
  );
}

const base: React.CSSProperties = { boxSizing: 'border-box', cursor: 'pointer', textDecoration: 'none', fontFamily: 'inherit' };

function bigStyle(primary: boolean): React.CSSProperties {
  return {
    ...base,
    display: 'block',
    width: '100%',
    textAlign: 'left',
    padding: primary ? '11px 14px' : '9px 14px',
    borderRadius: 8,
    background: primary ? THEME_BRAND : 'transparent',
    color: primary ? '#0f0f14' : '#e4e4e7',
    border: primary ? `1px solid ${THEME_BRAND}` : '1px solid #3f3f46',
  };
}

const mutedStyle: React.CSSProperties = {
  ...base,
  display: 'inline-flex',
  alignItems: 'center',
  minHeight: 34,
  padding: '0 11px',
  borderRadius: 6,
  fontSize: 12,
  fontWeight: 600,
  background: 'transparent',
  color: '#a1a1aa',
  border: '1px solid #34343b',
};

const quietStyle: React.CSSProperties = {
  ...base,
  alignSelf: 'center',
  minHeight: 34,
  padding: '0 8px',
  fontSize: 12,
  background: 'none',
  border: 'none',
  color: '#71717a',
};
