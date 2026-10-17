'use client';

import { create } from 'zustand';

// Which half of the Playground is showing: orchestration (when Ponds run) or Trickle (which rows a run
// touches). Each half keeps its own state while the other shows. The address follows the mode (`/` or
// `/trickle`) without a navigation, so switching keeps both halves' state.

export type Mode = 'orchestration' | 'trickle';

interface ModeState {
  mode: Mode | null; // null until the page sets it from its route
  setMode(mode: Mode): void;
}

export const useModeStore = create<ModeState>((set) => ({
  mode: null,
  setMode(mode) {
    set({ mode });
    const path = mode === 'trickle' ? '/trickle' : '/';
    if (typeof window !== 'undefined' && window.location.pathname !== path) {
      window.history.replaceState(null, '', path); // drop ?example= and the like: they'd re-apply on remount
    }
  },
}));
