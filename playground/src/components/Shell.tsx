'use client';

import { useModeStore, type Mode } from '@/lib/mode';
import { Playground } from './Playground';
import { TrickleApp } from './trickle/TrickleApp';

// The Playground's one page: orchestration or Trickle, switched in the sidebar. `initial` is the route's mode
// (`/` or `/trickle`); after that the store decides.
export function Shell({ initial }: { initial: Mode }) {
  const mode = useModeStore((s) => s.mode) ?? initial;
  return mode === 'trickle' ? <TrickleApp /> : <Playground />;
}
