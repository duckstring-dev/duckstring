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

## Test

```bash
npx playwright install chromium   # once
npm test                          # unit tests, plus the tour end to end at 360x640, 390x844 and 1280x800
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
