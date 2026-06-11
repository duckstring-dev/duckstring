---
title: Control
description: Wake, Sleep, Force, Kill — operating a Pond directly.
---

# Control

:::caution Work in progress
This page is a stub. Content to be written in a later pass.
:::

**Planned content:**

- `duckstring control {force|wake|sleep|kill} {pond}` and what each verb means in demand terms:
  - **Wake** — a one-shot, non-propagating pull: run once if Sources are already fresher, without soliciting them
  - **Force** — recompute now at the current freshness even with no upstream change; does not propagate downstream
  - **Sleep** — clear all demand; in-flight runs complete; cancels the standing trigger; `--upstream` reaches ancestors
  - **Kill** — terminate the worker immediately and park the Pond `killed` until cleared
- Which verbs clear a failed/killed state (wake, force, clear)
- Typical workflows: force after deploying a patch, sleep before maintenance, kill a runaway run
- Pointers: [Fault tolerance](fault-tolerance.md), [Triggers](triggers.md)
