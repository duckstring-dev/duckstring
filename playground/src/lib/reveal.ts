'use client';

// Scroll a control a tour points at into the part of its scroll container that the tour card leaves visible.
// Called when a step or wait opens (not continuously), so the viewer can scroll freely afterwards. On a phone the
// card sits over the screen's top or bottom; elsewhere it rarely overlaps the sidebar, and then nothing moves.

const GAP = 8; // from the card
const RING = 10; // the highlight ring sits 7px outside the control (3px outline, 4px offset)

function scrollParent(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight) return p;
  }
  return null;
}

export function revealClear(selector: string) {
  const el = [...document.querySelectorAll<HTMLElement>(selector)].find((e) => e.getBoundingClientRect().height > 0);
  if (!el) return;
  const scroller = scrollParent(el);
  if (!scroller) return;
  const s = scroller.getBoundingClientRect();
  let top = Math.max(s.top, 0) + RING;
  let bottom = Math.min(s.bottom, window.innerHeight) - RING;
  const card = document.getElementById('ds-tour-card')?.getBoundingClientRect();
  if (card && card.height > 0 && card.left < s.right && card.right > s.left && card.top < bottom && card.bottom > top) {
    // The free band is the larger of what's left above and below the card.
    const above = card.top - GAP - top;
    const below = bottom - (card.bottom + GAP);
    if (above >= below) bottom = card.top - GAP - RING;
    else top = card.bottom + GAP + RING;
  }
  const box = el.getBoundingClientRect();
  let delta = 0;
  if (box.height > bottom - top || box.top < top) delta = box.top - top; // too tall to fit: show its top
  else if (box.bottom > bottom) delta = box.bottom - bottom;
  if (Math.abs(delta) < 1) return;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  scroller.scrollBy({ top: delta, behavior: reduce ? 'auto' : 'smooth' });
}

// Reveal once the card has laid out, and again after its size settles (a step's card, or a wait's pill).
export function revealSoon(selector: string, later: (ms: number, fn: () => void) => void = (ms, fn) => void setTimeout(fn, ms)) {
  later(250, () => revealClear(selector));
  later(700, () => revealClear(selector));
}
