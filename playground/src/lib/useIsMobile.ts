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
