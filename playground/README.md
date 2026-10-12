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

## Pulse release (experimental)

**Release** in the sim controls (or the sidebar with nothing selected) switches on an experimental
rule for stacked Pulses. A Pond counts the demands that reach it during each run of its own Ripples
and its Sources. Two in one run means that run can't keep up, so the Pond holds further demand
(still meeting it itself) and forwards one Pulse, at the current time, each time the slow run turns
over. A Source has turned over once it is idle or one of its first Ripples starts later work, and
with release on an Inlet doesn't start a Pond Run while its first Ripples are busy (so its freshness
stamp matches when it reads; later Ripples still pipeline). Held Ponds show a `holding N` or `saturated` badge, the console logs each hold and release,
and a Pond's chart adds the data age at each completion. A Tide of 0s with release on behaves like a
Wave.

The sidebar's **Examples** load the topologies it was explored on, and a setup can be opened from a
link: `?example=deep|decreasing|sibling|diamond|diamondlead|long|demo`, `?release=1`, and `?tide=<seconds>` or `?wave=1`
for a trigger on every Outlet. For example `/?tour=0&example=deep&release=1&tide=1`.

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
