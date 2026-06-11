---
title: Deploying
description: Ship a Pond version to a Catchment.
---

# Deploying

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- `duckstring pond deploy` from a Pond's project root; `--all` for a directory of Ponds
- What a deploy does: registers an immutable `pond_version` artifact and atomically selects it for its major
- Deploy order doesn't matter: a sink can deploy before its source
- Upgrading: patch/minor swaps in place; a new major runs alongside the old until consumers move
- Redeploying a fix auto-clears a failure episode
- Cycle detection across Ponds (deploys that would create an inter-Pond cycle are rejected)
- Pointers: [Versioning](../concepts/versioning.md), [Fault tolerance](fault-tolerance.md)
