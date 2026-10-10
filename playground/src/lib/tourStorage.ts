// The tour's remembered choice and its URL switch. Kept free of React and the stores so it can be
// unit tested on its own.

export const TOUR_STORAGE_KEY = 'ds-playground-tour';

export type TourMemory = 'done' | 'skipped';

// `window.localStorage` itself can throw (blocked site data), so even reaching it is guarded.
function defaultStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

// The remembered choice, or null. Any failure reads as a first visit.
export function readTourMemory(storage: Storage | null = defaultStorage()): TourMemory | null {
  try {
    const v = storage?.getItem(TOUR_STORAGE_KEY);
    return v === 'done' || v === 'skipped' ? v : null;
  } catch {
    return null;
  }
}

// Remember the choice. A failure is ignored: the visitor just sees the prompt again next time.
export function writeTourMemory(value: TourMemory, storage: Storage | null = defaultStorage()): void {
  try {
    storage?.setItem(TOUR_STORAGE_KEY, value);
  } catch {
    /* storage unavailable */
  }
}

// `?tour=1` starts the tour whatever was remembered; `?tour=0` suppresses the prompt (for embedding).
export function tourParam(search: string): 'start' | 'suppress' | null {
  const v = new URLSearchParams(search).get('tour');
  if (v === '1') return 'start';
  if (v === '0') return 'suppress';
  return null;
}

// What to do on page load: start the tour, offer it, or stay out of the way.
export function initialTourMode(search: string, storage: Storage | null = defaultStorage()): 'start' | 'prompt' | 'none' {
  const param = tourParam(search);
  if (param === 'start') return 'start';
  if (param === 'suppress') return 'none';
  return readTourMemory(storage) ? 'none' : 'prompt';
}
