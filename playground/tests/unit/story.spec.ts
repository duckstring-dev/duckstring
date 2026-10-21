import { test, expect } from '@playwright/test';
import { usePlaygroundStore } from '../../src/lib/store';
import { inSwitchWindow, setUpStory } from '../../src/lib/story';
import { pondByName } from '../../src/lib/tourSteps';

// The story switches `reports` from every 6 s to continuous only inside inSwitchWindow, because other moments can
// lock the throttle into a rhythm about 2 s staler (bench/launch_alignment). This drives the real store through the
// story's setup and, for switch attempts starting at every point of `forecast`'s 18 s cycle, checks the switch
// always lands in the fresher rhythm, and that continuous is then fresher than every 6 s was.

const get = usePlaygroundStore.getState;

function meanAge(id: string, from: number): number {
  const p = get().pondStates[id];
  const C = p.completionTimes;
  const A = p.ages ?? [];
  const off = C.length - A.length;
  let area = 0;
  let span = 0;
  for (let i = 1; i < A.length; i++) {
    if (C[i + off - 1] < from) continue;
    const g = C[i + off] - C[i + off - 1];
    area += (A[i - 1] + g / 2) * g;
    span += g;
  }
  return area / span;
}

function advance(ms: number) {
  const end = get().now + ms;
  while (get().now < end) get().tick(get().now + 100);
}

test('the continuous switch always lands in the fresher rhythm', () => {
  for (let offset = 0; offset < 18000; offset += 600) {
    setUpStory(get);
    const reports = pondByName(get(), 'reports')!;
    get().setRefresh(reports, { everyMs: 6000 });
    get().setRefresh(pondByName(get(), 'forecast')!, { everyMs: 18000 });
    advance(36000 + offset);
    const every6 = meanAge(reports, get().now - 18000);
    let waited = 0;
    while (!inSwitchWindow(get()) && waited < 10000) {
      advance(100);
      waited += 100;
    }
    expect(inSwitchWindow(get()), `offset ${offset}: no switch window within 10 s`).toBe(true);
    get().setRefresh(reports, { everyMs: 0 });
    advance(60000);
    const continuous = meanAge(reports, get().now - 30000);
    expect(continuous, `offset ${offset}: continuous ${continuous} ms against every 6 s ${every6} ms`).toBeLessThan(9000);
    expect(continuous).toBeLessThan(every6);
  }
});

test('outside the window, the switch can lock the staler rhythm (so the window matters)', () => {
  setUpStory(get);
  const reports = pondByName(get(), 'reports')!;
  get().setRefresh(reports, { everyMs: 6000 });
  get().setRefresh(pondByName(get(), 'forecast')!, { everyMs: 18000 });
  advance(36000);
  const since = () => {
    const done = get().pondStates[reports].completionTimes;
    return get().now - done[done.length - 1];
  };
  while (!(since() >= 1200 && since() < 1500)) advance(100);
  get().setRefresh(reports, { everyMs: 0 });
  advance(60000);
  expect(meanAge(reports, get().now - 30000)).toBeGreaterThan(9500);
});
