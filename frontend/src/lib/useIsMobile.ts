'use client';

import { useSyncExternalStore } from 'react';

// Phone-portrait breakpoint: below this the sidebar becomes a bottom sheet and
// tapping a node zooms to it. Tablets and landscape phones keep the desktop layout.
// (Kept in lockstep with playground/src/lib/useIsMobile.ts.)
const QUERY = '(max-width: 768px)';

function subscribe(onChange: () => void): () => void {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
}

export function useIsMobile(): boolean {
  // Server snapshot is false → desktop markup on first paint, corrected at hydration.
  return useSyncExternalStore(subscribe, () => window.matchMedia(QUERY).matches, () => false);
}

// A short screen (a phone held landscape): the desktop layout, but the bottom panels fold away.
const SHORT = '(max-height: 560px)';

function subscribeShort(onChange: () => void): () => void {
  const mql = window.matchMedia(SHORT);
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
}

export function useIsShort(): boolean {
  return useSyncExternalStore(subscribeShort, () => window.matchMedia(SHORT).matches, () => false);
}
