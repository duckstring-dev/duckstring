import { test, expect } from '@playwright/test';
import { TOUR_STORAGE_KEY, initialTourMode, readTourMemory, tourParam, writeTourMemory } from '../../src/lib/tourStorage';

// An in-memory Storage, and one whose every call throws (blocked site data, some private windows).
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, String(v)),
  };
}

function blockedStorage(): Storage {
  const fail = () => {
    throw new DOMException('blocked', 'SecurityError');
  };
  return { length: 0, clear: fail, getItem: fail, key: fail, removeItem: fail, setItem: fail };
}

test('a remembered choice round-trips', () => {
  const s = memoryStorage();
  expect(readTourMemory(s)).toBeNull();
  writeTourMemory('skipped', s);
  expect(s.getItem(TOUR_STORAGE_KEY)).toBe('skipped');
  expect(readTourMemory(s)).toBe('skipped');
  writeTourMemory('done', s);
  expect(readTourMemory(s)).toBe('done');
});

test('an unknown stored value reads as a first visit', () => {
  const s = memoryStorage();
  s.setItem(TOUR_STORAGE_KEY, 'maybe');
  expect(readTourMemory(s)).toBeNull();
});

test('blocked storage behaves like a first visit', () => {
  const s = blockedStorage();
  expect(readTourMemory(s)).toBeNull();
  expect(() => writeTourMemory('done', s)).not.toThrow();
  expect(initialTourMode('', s)).toBe('prompt');
  expect(readTourMemory(null)).toBeNull();
  expect(initialTourMode('', null)).toBe('prompt');
});

test('?tour= parses', () => {
  expect(tourParam('')).toBeNull();
  expect(tourParam('?tour=1')).toBe('start');
  expect(tourParam('?x=2&tour=0')).toBe('suppress');
  expect(tourParam('?tour=yes')).toBeNull();
});

test('?tour= overrides the remembered choice', () => {
  const s = memoryStorage();
  expect(initialTourMode('', s)).toBe('prompt');
  writeTourMemory('done', s);
  expect(initialTourMode('', s)).toBe('none');
  expect(initialTourMode('?tour=1', s)).toBe('start');
  expect(initialTourMode('?tour=0', memoryStorage())).toBe('none');
});
