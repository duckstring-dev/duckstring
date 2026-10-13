# Duckstring Playground

An in-browser, in-memory simulation of the Duckstring orchestration model — build a graph of
Ponds and Ripples, send Taps/Pulses/Waves/Tides, and watch freshness propagate. It runs the
TypeScript reference engine (`src/lib/orchestration.ts`) entirely client-side; there is no backend
and it makes no network calls.

This is a standalone app, intended to be deployed on its own (e.g. `playground.duckstring.com`).

## Develop

```bash
npm install
npm run dev      # http://localhost:3000
```

## Guided tour

A first visit is offered a short guided tour (three chapters), and the **Tour** button in the
sidebar header restarts it. The choice is remembered in `localStorage` (`ds-playground-tour`).
`?tour=1` starts the tour regardless, and `?tour=0` suppresses the offer (for embedding).

The steps are data in `src/lib/tourSteps.ts`; the runner is `src/lib/tour.ts`.

## Pulse throttle

Pulses that arrive faster than the pipeline can serve them are paced by the bottleneck. A Ripple that
sees two demands arrive during one of its runs is slow until it goes idle, and its Pond and every Pond
downstream are throttled (it passes down like Blocked, through required Sources only). Then:

- a throttled Outlet still takes every Pulse as its own target, but sends at most one Pulse upstream
  per completion;
- any other Pond with a slow Ripple holds the demand reaching it and, while it has demand to meet,
  sends one Pulse upstream each time that Ripple starts a run.

So new demand enters at the rate the Outlet is served and travels upstream timed to the bottleneck's
starts. Sparse Pulses are never affected. An Inlet doesn't start a Pond Run while its first Ripples are
busy (so its freshness stamp matches when it reads), and a demand for a time a run already in flight
covers counts as met.

Ponds show `slow`, `throttled` or `holding N` badges, the console logs each change, and a Pond's chart
adds the data age at each completion. A Tide of 0s behaves like a Wave.

The sidebar's **Examples** load the topologies it was explored on, and a setup can be opened from a
link: `?example=deep|decreasing|sibling|diamond|diamondlead|long|demo`, and `?tide=<seconds>` or
`?wave=1` for a trigger on every Outlet. For example `/?tour=0&example=diamond&tide=1`.

## Test

```bash
npx playwright install chromium   # once
npm test                          # unit tests, plus the tour end to end at 360x640, 390x844, 844x390 and 1280x800
```

The end-to-end run saves a screenshot per tour step under `test-results/`.

## Build

```bash
npm run build
npm run start
```

## Deploy (Vercel)

Vercel detects Next.js automatically — no extra configuration is required. Point a project at this
directory (or its repository root) and deploy; the default build command (`next build`) and output
are picked up as-is.
