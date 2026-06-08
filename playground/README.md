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

## Build

```bash
npm run build
npm run start
```

## Deploy (Vercel)

Vercel detects Next.js automatically — no extra configuration is required. Point a project at this
directory (or its repository root) and deploy; the default build command (`next build`) and output
are picked up as-is.
