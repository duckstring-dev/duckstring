---
title: Architecture
description: How the runtime is put together — Catchment, Ducks, and the engine.
---

# Architecture

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- The two-tier runtime: the Catchment runs the full orchestration engine (Ponds + Ripples, pull + push) and decides Pond Runs; each executing Pond runs a **Duck** — a per-Pond worker process that pushes the Pond's Ripples to the run frontier
- The pure engine (`duckstring.engine`): no FastAPI/DB/HTTP — the state machine from [Theory](../theory.md), shared by Catchment and Duck
- Transport: Ducks always dial back (events POST + held job poll), so local and remote workers run the same code
- Duck lifecycle: spawned on first run, kept warm under a standing trigger, killed when idle; survives Catchment downtime via its per-Pond ledger and idempotent event replay
- Storage: the Catchment SQLite database (identity / topology / live state / history), per-Pond ledgers, per-Pond DuckDB registries, and Parquet exports for cross-Pond reads
- No concurrency cap: completions clock the pull cascade
- Restart story: state restore, resuming incomplete runs, liveness checking
