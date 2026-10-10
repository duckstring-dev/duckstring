'use client';

import { useEffect, useRef } from 'react';
import { useTourStore } from '@/lib/tour';

// Guides the eye to each step's target: a white pill fades in as it contracts onto it, with the rest of
// the screen dimming slightly, then fades, leaving the steady white ring (.ds-tour-ring/.ds-tour-outline in
// globals.css). White, because every other colour on the canvas already means a state. It never
// takes pointer events, so the canvas and the bottom sheet stay usable underneath.

const DURATION_MS = 900;
const FADE_MS = 200;
const PAD = 6; // gap between the landed pill and the target
const RADIUS = 12; // the landed pill's corner radius
const START = 160; // how far the pill starts out from the target on every side
const DIM = 0.4; // the dim's strongest opacity, mid-contraction

// The box around every element matching `selector` (a control can be marked on more than one).
function targetRect(selector: string): DOMRect | null {
  let r: { l: number; t: number; r: number; b: number } | null = null;
  for (const el of document.querySelectorAll(selector)) {
    const b = el.getBoundingClientRect();
    if (!b.width || !b.height) continue;
    r = r
      ? { l: Math.min(r.l, b.left), t: Math.min(r.t, b.top), r: Math.max(r.r, b.right), b: Math.max(r.b, b.bottom) }
      : { l: b.left, t: b.top, r: b.right, b: b.bottom };
  }
  return r && new DOMRect(r.l, r.t, r.r - r.l, r.b - r.t);
}

const easeOut = (p: number) => 1 - (1 - p) ** 3;
const smoothstep = (a: number, b: number, p: number) => {
  const x = Math.min(1, Math.max(0, (p - a) / (b - a)));
  return x * x * (3 - 2 * x);
};

// One contraction onto `selector`, starting after `delay`. It tracks the target every frame, since
// the canvas is usually still panning to it and the sidebar may be scrolling it into view.
function Contraction({ selector, delay }: { selector: string; delay: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const start = performance.now() + delay;
    let raf = 0;
    const frame = (now: number) => {
      const p = (now - start) / DURATION_MS;
      const rect = p >= 0 ? targetRect(selector) : null;
      if (p > 1 + FADE_MS / DURATION_MS) {
        el.style.display = 'none';
        return;
      }
      if (!rect) {
        el.style.opacity = '0';
        raf = requestAnimationFrame(frame);
        return;
      }
      const e = easeOut(Math.min(1, p));
      // Starts invisible and a little larger than the target, and gains opacity as it closes in.
      const inflate = (1 - e) * START;
      const w = rect.width + 2 * (PAD + inflate);
      const h = rect.height + 2 * (PAD + inflate);
      el.style.opacity = String(smoothstep(0, 0.6, p) * (1 - smoothstep(1, 1 + FADE_MS / DURATION_MS, p)));
      el.style.left = `${rect.left - PAD - inflate}px`;
      el.style.top = `${rect.top - PAD - inflate}px`;
      el.style.width = `${w}px`;
      el.style.height = `${h}px`;
      el.style.borderRadius = `${RADIUS + (1 - e) * (Math.min(w, h) / 2 - RADIUS)}px`;
      const dim = DIM * (1 - smoothstep(0.55, 1, p));
      el.style.boxShadow = `0 0 0 9999px rgba(0, 0, 0, ${dim.toFixed(3)}), 0 0 28px 6px rgba(255, 255, 255, 0.55)`;
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [selector, delay]);

  return (
    <div
      ref={ref}
      data-testid="tour-spotlight"
      aria-hidden
      style={{
        position: 'fixed',
        zIndex: 45, // over the canvas and sidebar, under the tour card
        pointerEvents: 'none',
        border: '3px solid #ffffff',
        boxSizing: 'border-box',
        opacity: 0,
      }}
    />
  );
}

export function TourSpotlight() {
  const phase = useTourStore((t) => t.phase);
  const index = useTourStore((t) => t.index);
  const ring = useTourStore((t) => t.ring);
  const outline = useTourStore((t) => t.outline);
  const reduceMotion = typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (phase !== 'step' || reduceMotion) return null;

  // The canvas node first, then the control the step is about. Keyed by step, so it replays on
  // every step even when the target doesn't change.
  const targets: { selector: string; delay: number }[] = [];
  if (ring) targets.push({ selector: '.ds-tour-ring', delay: 150 });
  if (outline) targets.push({ selector: '.ds-tour-outline', delay: ring ? 750 : 150 });
  return (
    <>
      {targets.map((t) => (
        <Contraction key={`${index}-${t.selector}`} selector={t.selector} delay={t.delay} />
      ))}
    </>
  );
}
