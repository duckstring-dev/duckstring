---
title: Playground
description: Try the orchestration model in your browser — nothing to install.
---

# Playground

The fastest way to understand Duckstring is to watch it run. The **[Playground](https://playground.duckstring.com)** is an in-browser simulation of the orchestration model: build a graph of Ponds and Ripples, send triggers, and watch freshness propagate. It runs entirely client-side — no install, no backend, no sign-up.

It uses the same state machine as the real runtime (the Python engine is a behaviour-for-behaviour port of the playground's reference implementation), so what you see is exactly how a deployed pipeline behaves — just with simulated work instead of real transforms.

## What you can do

The playground opens with the demo pipeline from the [Quickstart](quickstart.md) already loaded — `transactions` and `products` feeding `sales`, feeding `reports` — so you can start triggering immediately.

- **Send triggers.** Select a Pond and send a **Tap** (pull once), **Pulse** (push once), **Wave** (standing pull), or **Tide** (standing push with a staleness bound). Watch demand tokens flow upstream and runs cascade back down.
- **Edit the topology.** Add and delete Ponds, link them as Sources and Sinks, and add Ripples inside a Pond with their own parent links — the same graph you'd otherwise declare in code and `pond.toml`.
- **Tune the work.** Set each Ripple's duration (and a variability factor) to model fast and slow transforms, then watch where the bottleneck forms.
- **Add Windows.** Give an Inlet a recurring availability window and see how a downstream Wave throttles to it.
- **Control time.** Pause, change the simulation speed, and read the event console and trace chart to follow exactly what fired and why.

## Things worth trying

A few experiments that show off behaviour you won't get from a scheduler:

1. **Wave vs. the bottleneck.** Start a Wave on the terminal Pond. Every Ripple settles into a cadence set by the slowest Ripple upstream — nothing runs faster than its consumer can use, with no rate limits configured anywhere.
2. **Tap, twice.** Send one Tap and watch it propagate upstream; the upstream Ponds re-arm as it runs. Send a second Tap and notice it's supplied almost immediately — the pull model pre-positions fresh data for the next request.
3. **Tide vs. Wave.** Put a 10-second Tide on the Outlet, then swap it for a Wave with a 10-second Window on the Inlets. Same once-per-10s outcome — but the Window pins *when* the run happens to when the source data is actually available.
4. **Overlapping runs.** Make the pipeline longer than the trigger interval and watch multiple runs in flight at once. There is no concurrency cap; completions clock the cascade.

When the behaviour starts to make sense, [Theory](../theory.md) explains the rules behind it — and the [Quickstart](quickstart.md) gets the real thing running on your machine in a few minutes.
