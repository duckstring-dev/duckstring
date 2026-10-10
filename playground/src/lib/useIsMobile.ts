'use client';

import { useSyncExternalStore } from 'react';

// Phone-portrait breakpoint: below this the sidebar becomes a bottom sheet and
// tapping a node zooms to it. Tablets and landscape phones keep the desktop layout.
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

// A short screen (a phone in landscape): the desktop layout, but with little height to spare.
const SHORT_QUERY = '(max-height: 500px)';

function subscribeShort(onChange: () => void): () => void {
  const mql = window.matchMedia(SHORT_QUERY);
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
}

export function useIsShort(): boolean {
  return useSyncExternalStore(subscribeShort, () => window.matchMedia(SHORT_QUERY).matches, () => false);
}
