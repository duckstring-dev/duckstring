'use client';

import { useEffect, useRef } from 'react';
import { usePlaygroundStore, THEME_BRAND } from '@/lib/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { useTourStore } from '@/lib/tour';
import { TOUR_STEPS, CHAPTER_TITLES } from '@/lib/tourSteps';
import { initialTourMode } from '@/lib/tourStorage';

// The tour's card. On desktop and tablets it floats over the bottom-left of the canvas, clear of the
// sidebar. On a phone it spans the screen: above the bottom sheet when the step points at a sidebar
// control, at the bottom otherwise.

// `backticks` → code.
function Copy({ text }: { text: string }) {
  return (
    <>
      {text.split('`').map((part, i) =>
        i % 2 ? (
          <code key={i} style={{ color: '#e4e4e7', background: '#27272a', borderRadius: 3, padding: '0 4px' }}>
            {part}
          </code>
        ) : (
          part
        )
      )}
    </>
  );
}

function Button({
  onClick,
  children,
  primary = false,
  quiet = false,
  testId,
}: {
  onClick: () => void;
  children: React.ReactNode;
  primary?: boolean;
  quiet?: boolean;
  testId?: string;
}) {
  return (
    <button
      onClick={onClick}
      data-testid={testId}
      style={{
        minHeight: 44,
        padding: quiet ? '0 6px' : '0 14px',
        borderRadius: 6,
        fontSize: quiet ? 12 : 13,
        fontWeight: 600,
        cursor: 'pointer',
        fontFamily: 'inherit',
        letterSpacing: '0.02em',
        background: primary ? THEME_BRAND : 'transparent',
        color: primary ? '#0f0f14' : quiet ? '#71717a' : '#e4e4e7',
        border: primary ? `1px solid ${THEME_BRAND}` : quiet ? '1px solid transparent' : '1px solid #3f3f46',
      }}
    >
      {children}
    </button>
  );
}

// Progress through a wait, paced by its measured typical duration: linear to 90% at the expected
// time, then creeping towards the end, so a slow wait never looks finished or frozen.
function WaitProgress() {
  const wait = useTourStore((t) => t.wait);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!wait) return;
    let raf = 0;
    const frame = (now: number) => {
      const x = (now - wait.startedAt) / wait.expectedMs;
      const p = x < 0.9 ? x : 0.9 + 0.09 * (1 - Math.exp(-(x - 0.9) * 2));
      if (ref.current) ref.current.style.width = `${(Math.max(0, p) * 100).toFixed(2)}%`;
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [wait]);
  return (
    <div
      ref={ref}
      data-testid="tour-progress"
      role="progressbar"
      aria-label="Waiting for the simulation"
      style={{ position: 'absolute', left: 0, bottom: 0, height: 3, width: 0, background: THEME_BRAND }}
    />
  );
}

export function TourCard() {
  const isMobile = useIsMobile();
  const phase = useTourStore((t) => t.phase);
  const index = useTourStore((t) => t.index);
  const waitLabel = useTourStore((t) => t.waitLabel);
  const lastAdvance = useTourStore((t) => t.lastAdvance);
  const tour = useTourStore.getState;
  const cardRef = useRef<HTMLDivElement>(null);

  // First load: start (?tour=1), offer (first visit) or stay quiet.
  useEffect(() => {
    const mode = initialTourMode(window.location.search);
    if (mode === 'start') tour().start();
    else if (mode === 'prompt') tour().offer();
  }, [tour]);

  const step = TOUR_STEPS[index];

  // Move focus to the card when it opens, and bring the outlined control into view.
  useEffect(() => {
    if (phase !== 'step' && phase !== 'prompt' && phase !== 'confirm') return;
    cardRef.current?.focus({ preventScroll: true });
    if (phase !== 'step' || !step.target?.control) return;
    const t = setTimeout(() => {
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      document
        .querySelector(`[data-tour="${step.target!.control}"]`)
        ?.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
    }, 200);
    return () => clearTimeout(t);
  }, [phase, index, step]);

  // Escape skips; the arrow keys move. Typing in the sidebar's inputs is left alone.
  useEffect(() => {
    if (phase === 'off') return;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
      const t = tour();
      if (e.key === 'Escape') {
        if (t.phase === 'prompt' || t.phase === 'confirm') t.decline();
        else t.skip();
      } else if (e.key === 'ArrowRight' && t.phase === 'step') {
        t.primary();
      } else if (e.key === 'ArrowLeft' && t.phase === 'step') {
        t.back();
      } else if (e.key === 'ArrowRight' && t.phase === 'waiting') {
        t.hurry();
      } else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [phase, tour]);

  // The action buttons re-render with the store, so a done action shows as Next.
  const actionDone = usePlaygroundStore((s) => (step.action ? step.action.done(s) : true));

  if (phase === 'off') return null;

  // On a phone, a step pointing at the sidebar puts the card at the top, so the sheet stays visible.
  const atTop = isMobile && phase === 'step' && !!step.target?.control;
  const anchor: React.CSSProperties = isMobile
    ? {
        position: 'fixed',
        left: 8,
        right: 8,
        ...(atTop
          ? { top: 'calc(8px + env(safe-area-inset-top))' }
          : { bottom: 'calc(8px + env(safe-area-inset-bottom))' }),
      }
    : { position: 'absolute', left: 56, bottom: 16, width: 320 };
  const panel: React.CSSProperties = {
    ...anchor,
    zIndex: 50,
    background: '#18181d',
    border: '1px solid #3f3f46',
    borderRadius: 10,
    boxShadow: '0 12px 32px rgba(0, 0, 0, 0.55)',
    color: '#e4e4e7',
    fontFamily: 'ui-monospace, SFMono-Regular, monospace',
    outline: 'none',
  };

  if (phase === 'waiting') {
    return (
      <div id="ds-tour-card" data-testid="tour-pill" role="status" style={{ ...panel, padding: '4px 4px 4px 14px', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ flex: 1, fontSize: 12, color: '#a1a1aa' }}>{`${waitLabel ?? 'Running'}…`}</span>
          <Button onClick={() => tour().hurry()} testId="tour-hurry">
            Skip ahead
          </Button>
        </div>
        <WaitProgress />
      </div>
    );
  }

  if (phase === 'prompt' || phase === 'confirm') {
    const isPrompt = phase === 'prompt';
    return (
      <div
        id="ds-tour-card"
        ref={cardRef}
        tabIndex={-1}
        role="dialog"
        aria-label="Playground tour"
        data-testid={isPrompt ? 'tour-prompt' : 'tour-confirm'}
        style={{ ...panel, padding: 14 }}
      >
        <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>
          {isPrompt ? 'Want the 5-minute tour?' : 'Start the tour?'}
        </div>
        <div style={{ fontSize: 12, color: '#a1a1aa', lineHeight: 1.5, marginBottom: 12 }}>
          {isPrompt
            ? 'A short guided run through the example pipeline, in three chapters.'
            : 'The tour resets the Playground to the example pipeline, so your changes will be lost.'}
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={() => tour().decline()} testId="tour-decline">
            {isPrompt ? 'Skip' : 'Cancel'}
          </Button>
          <Button primary onClick={() => tour().start()} testId="tour-start">
            {isPrompt ? 'Start' : 'Reset and start'}
          </Button>
        </div>
      </div>
    );
  }

  const chapterSteps = TOUR_STEPS.filter((s) => s.chapter === step.chapter);
  const position = chapterSteps.indexOf(step) + 1;
  const isLast = index === TOUR_STEPS.length - 1;
  const chapterEnd = !isLast && TOUR_STEPS[index + 1].chapter !== step.chapter;
  const primaryLabel = isLast ? 'Close' : step.action && !actionDone ? step.action.label : chapterEnd ? 'Continue' : 'Next';

  return (
    <div
      id="ds-tour-card"
      ref={cardRef}
      tabIndex={-1}
      role="dialog"
      aria-label="Playground tour"
      data-testid="tour-card"
      data-step={step.id}
      data-last-advance={lastAdvance ?? undefined}
      // Header and buttons stay put; only the text scrolls if a phone runs out of height.
      style={{
        ...panel,
        padding: '10px 14px 12px',
        display: 'flex',
        flexDirection: 'column',
        maxHeight: isMobile ? '60dvh' : undefined,
      }}
    >
      <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', minHeight: 44, marginTop: -6 }}>
        <span style={{ fontSize: 10, fontWeight: 700, color: '#71717a', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
          Chapter {step.chapter} · {position} of {chapterSteps.length}
        </span>
        {!isLast && (
          <Button quiet onClick={() => tour().skip()} testId="tour-skip">
            Skip tour
          </Button>
        )}
      </div>
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        {position === 1 && (
          <div style={{ fontSize: 11, color: THEME_BRAND, marginBottom: 4 }}>{CHAPTER_TITLES[step.chapter]}</div>
        )}
        <div aria-live="polite">
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>{step.title}</div>
          <div style={{ fontSize: 12.5, color: '#c4c4cc', lineHeight: 1.55 }}>
            <Copy text={step.body} />
          </div>
          {step.suggestions && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18, listStyle: 'disc', fontSize: 12.5, color: '#c4c4cc', lineHeight: 1.55 }}>
              {step.suggestions.map((t) => (
                <li key={t} style={{ marginBottom: 4 }}>
                  <Copy text={t} />
                </li>
              ))}
            </ul>
          )}
          {step.footer && (
            <div style={{ fontSize: 12.5, color: '#c4c4cc', lineHeight: 1.55, marginTop: 6 }}>
              <Copy text={step.footer} />
            </div>
          )}
        </div>
        {step.links && (
          <div style={{ display: 'flex', gap: 14, marginTop: 10, fontSize: 13 }}>
            {step.links.map((l) => (
              <a key={l.href} href={l.href} target="_blank" rel="noopener noreferrer" style={{ color: THEME_BRAND, textDecoration: 'underline' }}>
                {l.label}
              </a>
            ))}
          </div>
        )}
      </div>
      <div style={{ flexShrink: 0, display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 8, marginTop: 12 }}>
        {index > 0 && (
          <Button onClick={() => tour().back()} testId="tour-back">
            Back
          </Button>
        )}
        <span style={{ flex: 1 }} />
        {chapterEnd && (
          <Button onClick={() => tour().finish()} testId="tour-finish">
            Finish
          </Button>
        )}
        <Button primary onClick={() => tour().primary()} testId="tour-next">
          {primaryLabel}
        </Button>
      </div>
    </div>
  );
}
