---
title: Versioning
description: SemVer for transforms — atomic upgrades and concurrent major versions.
---

# Versioning

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- SemVer on Ponds: what a major / minor / patch bump means for consumers
- One selected version per `(pond, major)`: a deploy upserts the selection atomically
- Concurrent major versions: the earlier version keeps executing until no consumer depends on it
- Sinks pin a Source by name + major — a sink can even deploy before its source
- Breaking changes without organisation-wide coordination (the contrast with mesh patterns)
- Redeploying a fixed artifact auto-clears a failure episode
- Pointers: [Deploying](../guides/deploying.md), [pond.toml reference](../reference/pond-toml.md)
