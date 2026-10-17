'use client';

import Link from 'next/link';
import { THEME_BRAND } from '@/lib/store';
import type { RunEvents } from '@/lib/trickle/pipeline';
import { useTrickleStore } from '@/lib/trickle/store';
import { useTrickleTour } from '@/lib/trickle/tour';
import { chip } from './Figures';
import { Legend, MONO } from './theme';

// What the next run brings, how much of each table the last run touched, and the way back. A bottom sheet on
// a phone, a column on the right otherwise.

const EVENTS: { key: keyof RunEvents; label: string }[] = [
  { key: 'newOrders', label: 'New orders' },
  { key: 'reprice', label: 'Price changes' },
  { key: 'discontinue', label: 'Remove a product' },
];

// `part` is null until the table's phase of the run has come.
function TookPart({ label, part, whole, testId }: { label: string; part: number | null; whole: number; testId: string }) {
  const frac = whole && part ? Math.min(1, part / whole) : 0;
  return (
    <div data-testid={testId} style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', fontSize: 11, color: '#a1a1aa', marginBottom: 4 }}>
        <span style={{ color: '#e4e4e7' }}>{label}</span>
        <span style={{ marginLeft: 'auto' }}>
          {part === null ? '–' : `${part.toLocaleString()} of ${whole.toLocaleString()}`}
        </span>
      </div>
      <div style={{ height: 8, background: '#2c2b30', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{ width: `${Math.max(frac * 100, part ? 0.8 : 0)}%`, height: '100%', background: THEME_BRAND, transition: 'width 300ms' }} />
      </div>
    </div>
  );
}

function Content() {
  const events = useTrickleStore((s) => s.events);
  const toggleEvent = useTrickleStore((s) => s.toggleEvent);
  const ringed = useTrickleStore((s) => s.ring.includes('events'));
  const result = useTrickleStore((s) => s.result);
  const reset = useTrickleStore((s) => s.reset);
  const phase = useTrickleStore((s) => s.phase);
  const pricedDone = phase !== 'sources';
  const revenueDone = pricedDone && phase !== 'join';
  const play = useTrickleStore((s) => s.play);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <section>
        <h3 style={h3}>Each run brings</h3>
        <div data-tour="events" className={ringed ? 'ds-tour-outline' : undefined} style={{ display: 'flex', flexWrap: 'wrap', gap: 6, padding: 2 }}>
          {EVENTS.map(({ key, label }) => (
            <button key={key} role="switch" aria-checked={events[key]} onClick={() => toggleEvent(key)} style={chip(events[key])}>
              {events[key] ? '✓ ' : ''}
              {label}
            </button>
          ))}
        </div>
      </section>

      <section>
        <h3 style={h3}>{result ? `Rows that took part in run ${result.run}` : 'Rows that took part'}</h3>
        {result ? (
          <>
            <TookPart testId="took-priced" label="priced" part={pricedDone ? result.joinRows : null} whole={result.joinFull} />
            <TookPart testId="took-revenue" label="revenue" part={revenueDone ? result.aggRows : null} whole={result.aggFull} />
            <div style={{ fontSize: 11, color: '#71717a', lineHeight: 1.5 }}>
              {result.bootstrap
                ? 'The first run computes everything.'
                : 'Out of what a full recompute would read. A run reads its sources’ changes and the rows they match.'}
            </div>
          </>
        ) : (
          <div style={{ fontSize: 11, color: '#71717a' }}>Nothing has run yet.</div>
        )}
      </section>

      <section>
        <h3 style={h3}>Squares</h3>
        <Legend marks={['base', 'read', 'added', 'updated', 'removed']} />
      </section>

      <section style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <button
          onClick={() => {
            reset();
            play();
          }}
          style={chip(false)}
        >
          Start again
        </button>
        <a href="https://docs.duckstring.com/concepts/trickles" target="_blank" rel="noopener noreferrer" style={{ ...chip(false), textDecoration: 'none', display: 'inline-flex', alignItems: 'center' }}>
          Docs ↗
        </a>
      </section>
    </div>
  );
}

function Header() {
  const start = useTrickleTour((t) => t.start);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <Link href="/" onClick={(e) => e.stopPropagation()} style={{ fontSize: 11, color: '#71717a', textDecoration: 'none' }}>
        ← Playground
      </Link>
      <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.12em' }}>TRICKLE</span>
      <span style={{ flex: 1 }} />
      <button
        data-testid="trickle-tour-button"
        onClick={(e) => {
          e.stopPropagation();
          start();
        }}
        style={{ background: 'transparent', border: `1px solid ${THEME_BRAND}`, color: THEME_BRAND, borderRadius: 5, padding: '2px 10px', fontSize: 11, fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit' }}
      >
        Tour
      </button>
    </div>
  );
}

export function TrickleSidebar({ mobile }: { mobile: boolean }) {
  const open = useTrickleStore((s) => s.sheetOpen);
  const setOpen = (f: (o: boolean) => boolean) => useTrickleStore.getState().setSheetOpen(f(open));
  const shell: React.CSSProperties = { background: '#16161b', color: '#e4e4e7', fontFamily: MONO };
  if (mobile) {
    return (
      <div style={{ ...shell, borderTop: '1px solid #3f3f46', flexShrink: 0, maxHeight: open ? '48dvh' : undefined, display: 'flex', flexDirection: 'column' }}>
        <div onClick={() => setOpen((o) => !o)} style={{ padding: '8px 14px', cursor: 'pointer', userSelect: 'none', display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, color: '#71717a' }}>{open ? '▾' : '▸'}</span>
          <div style={{ flex: 1 }}>
            <Header />
          </div>
        </div>
        {open && <div style={{ overflowY: 'auto', padding: '4px 14px 14px' }}><Content /></div>}
      </div>
    );
  }
  return (
    <div style={{ ...shell, width: 300, flexShrink: 0, borderLeft: '1px solid #3f3f46', padding: 18, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 18 }}>
      <Header />
      <Content />
    </div>
  );
}

const h3: React.CSSProperties = { margin: '0 0 8px', fontSize: 11, fontWeight: 700, color: '#71717a', letterSpacing: '0.08em', textTransform: 'uppercase' };
