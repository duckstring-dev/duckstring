'use client';

import { useEffect, useRef } from 'react';
import { THEME_BRAND } from '@/lib/store';
import { useTrickleStore } from '@/lib/trickle/store';
import { CHAPTERS, TRICKLE_STEPS, tourWasDone, useTrickleTour, type Figure } from '@/lib/trickle/tour';
import { useIsMobile, useIsShort } from '@/lib/useIsMobile';
import { CombineRule, JoinRule, WeightedRows } from './Figures';
import { JoinGrid } from './JoinGrid';
import { KeySet } from './KeySet';
import { MONO } from './theme';

// The tour's card. Chapter 1 cards sit in the middle of the canvas with a figure, over a dimmed pipeline.
// Chapter 2 cards float at the bottom left (the bottom on a phone, or the top when pointing at the sidebar)
// so the pipeline stays in view.

function Copy({ text }: { text: string }) {
  return (
    <>
      {text.split('`').map((part, i) =>
        i % 2 ? (
          <code key={i} style={{ color: '#e4e4e7', background: '#27272a', borderRadius: 3, padding: '0 4px', whiteSpace: 'nowrap' }}>
            {part}
          </code>
        ) : (
          part.split('**').map((bit, j) => (j % 2 ? <strong key={`${i}-${j}`} style={{ color: '#e4e4e7' }}>{bit}</strong> : bit))
        )
      )}
    </>
  );
}

function FigureView({ figure }: { figure: Figure }) {
  switch (figure.kind) {
    case 'weights':
      return <WeightedRows />;
    case 'combine':
      return <CombineRule />;
    case 'multiply':
      return <JoinRule />;
    case 'grid':
      return <JoinGrid step={figure.step} />;
    case 'keys':
      return <KeySet narrow={figure.narrow} />;
  }
}

function Button({ onClick, children, primary, quiet, testId }: {
  onClick: () => void; children: React.ReactNode; primary?: boolean; quiet?: boolean; testId?: string;
}) {
  return (
    <button
      onClick={onClick}
      data-testid={testId}
      style={{
        minHeight: 40,
        padding: quiet ? '0 6px' : '0 14px',
        borderRadius: 6,
        fontSize: quiet ? 12 : 13,
        fontWeight: 600,
        cursor: 'pointer',
        fontFamily: 'inherit',
        whiteSpace: 'nowrap',
        background: primary ? THEME_BRAND : 'transparent',
        color: primary ? '#0f0f14' : quiet ? '#71717a' : '#e4e4e7',
        border: primary ? `1px solid ${THEME_BRAND}` : quiet ? '1px solid transparent' : '1px solid #3f3f46',
      }}
    >
      {children}
    </button>
  );
}

let autoStarted = false; // the first-visit logic runs once per page load

export function TrickleTourCard() {
  const isMobile = useIsMobile();
  const open = useTrickleTour((t) => t.open);
  const index = useTrickleTour((t) => t.index);
  const tour = useTrickleTour.getState;
  useTrickleStore((s) => s.result); // re-render with each run, for steps whose copy quotes it
  const ref = useRef<HTMLDivElement>(null);
  const short = useIsShort();
  const step0 = TRICKLE_STEPS[index];
  const floating = open && step0.chapter === 2;
  const atTop0 = isMobile && (step0.ring ?? []).includes('events');

  // While the card floats over the pipeline, tell the canvas how much of it the card covers, so the graph is
  // framed in the rest.
  useEffect(() => {
    const card = ref.current;
    const canvas = document.getElementById('trickle-canvas');
    if (!floating || !card || !canvas) {
      tour().setInset({ top: 0, bottom: 0 });
      return;
    }
    const measure = () => {
      const c = card.getBoundingClientRect();
      const k = canvas.getBoundingClientRect();
      const covered = atTop0 ? Math.max(0, c.bottom - k.top) : Math.max(0, k.bottom - c.top);
      const px = Math.round(covered / 8) * 8; // in steps, so text reflowing by a line doesn't re-frame
      tour().setInset(atTop0 ? { top: px, bottom: 0 } : { top: 0, bottom: px });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(card);
    return () => ro.disconnect();
  }, [floating, atTop0, index, tour]);

  // First visit: open the tour (?tour=0 opts out, ?tour=1 forces it). Otherwise the pipeline just runs.
  useEffect(() => {
    if (autoStarted) {
      if (!tour().open) useTrickleStore.getState().play(); // back from orchestration mode
      return;
    }
    autoStarted = true;
    const param = new URLSearchParams(window.location.search).get('tour');
    if (param === '1' || (param !== '0' && !tourWasDone())) tour().start();
    else useTrickleStore.getState().play();
  }, [tour]);

  useEffect(() => {
    if (!open) return;
    ref.current?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') tour().close();
      else if (e.key === 'ArrowRight') tour().next();
      else if (e.key === 'ArrowLeft') tour().back();
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, index, tour]);

  if (!open) return null;
  const step = TRICKLE_STEPS[index];
  const centred = step.chapter === 1;
  const inChapter = TRICKLE_STEPS.filter((s) => s.chapter === step.chapter);
  const position = inChapter.indexOf(step) + 1;
  const isLast = index === TRICKLE_STEPS.length - 1;
  const chapterEnd = !isLast && TRICKLE_STEPS[index + 1].chapter !== step.chapter;
  const body = typeof step.body === 'function' ? step.body(useTrickleStore.getState()) : step.body;
  const atTop = atTop0;
  // A short screen (a phone in landscape): a chapter 2 card is a flat bar along the bottom.
  const flat = short && !isMobile && !centred;

  const place: React.CSSProperties = isMobile
    ? { position: 'fixed', left: 8, right: 8, ...(atTop ? { top: 8 } : { bottom: 'calc(8px + env(safe-area-inset-bottom))' }), maxHeight: centred ? '82dvh' : '42dvh' }
    : centred
      ? { position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%, -50%)', width: 440, maxHeight: '86%' }
      : flat
        ? { position: 'absolute', left: 0, right: 0, bottom: 0, maxHeight: '48%', borderRadius: '10px 10px 0 0' }
        : { position: 'absolute', left: 16, bottom: 16, width: 360 };

  return (
    <>
      {centred && <div aria-hidden style={{ position: isMobile ? 'fixed' : 'absolute', inset: 0, background: 'rgba(8, 8, 12, 0.55)', zIndex: 40 }} />}
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-label="Trickle tour"
        data-testid="trickle-tour"
        data-step={step.id}
        style={{
          ...place,
          zIndex: 50,
          display: 'flex',
          flexDirection: 'column',
          background: '#18181d',
          border: '1px solid #3f3f46',
          borderRadius: 10,
          ...(flat ? { borderRadius: '10px 10px 0 0', borderBottom: 'none' } : {}),
          boxShadow: '0 12px 32px rgba(0, 0, 0, 0.55)',
          color: '#e4e4e7',
          fontFamily: MONO,
          padding: flat ? '4px 14px 8px' : '8px 14px 12px',
          outline: 'none',
        }}
      >
        <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', minHeight: flat ? 30 : 36 }}>
          <span style={{ fontSize: 10, fontWeight: 700, color: '#71717a', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
            {CHAPTERS[step.chapter]} · {position} of {inChapter.length}
          </span>
          <span style={{ flex: 1 }} />
          {!isLast && (
            <Button quiet onClick={() => tour().close()} testId="trickle-tour-skip">
              Skip tour
            </Button>
          )}
        </div>
        <div style={flat ? { flex: 1, minHeight: 0, display: 'flex', gap: 16 } : { display: 'contents' }}>
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          <div aria-live="polite">
            <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 6 }}>{step.title}</div>
            <div style={{ fontSize: 12.5, color: '#c4c4cc', lineHeight: 1.55 }}>
              <Copy text={body} />
            </div>
          </div>
          {step.figure && (
            <div style={{ marginTop: 12, background: '#111115', border: '1px solid #2c2c33', borderRadius: 8, padding: 10 }}>
              <FigureView figure={step.figure} />
            </div>
          )}
          {step.links && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14, marginTop: 10, fontSize: 13 }}>
              {step.links.map((l) => (
                <a key={l.href} href={l.href} target={l.href.startsWith('http') ? '_blank' : undefined}
                  rel="noopener noreferrer" style={{ color: THEME_BRAND, textDecoration: 'underline' }}>
                  {l.label}
                </a>
              ))}
            </div>
          )}
        </div>
        <div style={flat
          ? { flexShrink: 0, width: 110, alignSelf: 'flex-end', display: 'flex', flexDirection: 'column-reverse', gap: 6 }
          : { flexShrink: 0, display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
          {index > 0 && (
            <Button onClick={() => tour().back()} testId="trickle-tour-back">
              Back
            </Button>
          )}
          {!flat && <span style={{ flex: 1 }} />}
          <Button primary onClick={() => tour().next()} testId="trickle-tour-next">
            {isLast ? 'Close' : chapterEnd ? 'Watch it run' : 'Next'}
          </Button>
        </div>
        </div>
      </div>
    </>
  );
}
