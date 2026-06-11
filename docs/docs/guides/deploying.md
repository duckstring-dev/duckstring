---
title: Deploying
description: Ship a Pond version to a Catchment.
---

# Deploying

Deploying is how a Pond version reaches a [Catchment](running-a-catchment.md) — the packaging-world equivalent of publishing a release. Deploys are atomic, per-Pond, and order-independent.

## Deploy a Pond

From the Pond's project root:

```bash
duckstring pond deploy
```

The CLI reads `pond.toml`, packages the project (excluding `.git`, virtualenvs, caches, and other noise), and uploads it. Before uploading it tells you whether this name+version already exists on the Catchment — redeploying an existing version overwrites it, *including its run history*, so version numbers should move forward once a version has run in earnest.

Options:

```bash
duckstring pond deploy -c prod        # target a specific Catchment
duckstring pond deploy --yes          # skip the confirmation
duckstring pond deploy --all --yes    # deploy every subdirectory containing a pond.toml
```

`--all` is the convenient way to bring up a whole pipeline at once, as in the [Quickstart](../getting-started/quickstart.md).

### Deploy from git

Instead of uploading the working directory, the Catchment can fetch the code itself from a git ref:

```bash
duckstring pond deploy --git v1.2.0      # a tag, branch, or commit
```

The CLI sends the project's `origin` remote URL and the ref; the Catchment clones and checks it out. This keeps deploys reproducible — the artifact is exactly what's in version control.

## What a deploy does

On receipt, the Catchment:

1. **Registers the version** as an immutable artifact — source snapshot, declared Sources, retry defaults, and the Ripple topology (discovered by importing `src/pond.py` and reading the `@ripple` registrations).
2. **Selects it** for its major line: "the Pond" for `(name, major)` now points at this version, atomically. Sinks on the same major pick it up from their next run.
3. **Validates the graph** — a deploy that would create a cycle between Ponds is rejected outright.
4. **Clears any failure** on the Pond: deploying a fix *is* the recovery action, so a [failed](fault-tolerance.md) Pond returns to service with the new artifact, no separate `clear` needed.

A few deliberate non-effects: deploying never starts a run (demand does — send a [trigger](triggers.md), or [`force`](control.md) the Pond to recompute immediately); and it never touches operational config — [retry budgets](fault-tolerance.md) are only *seeded* from `pond.toml` on the Pond's first deploy, and [Windows](windows.md) and triggers belong to the Catchment, so all of it survives redeploys.

## Order doesn't matter

Sources are declared by **name and major**, not by reference to a deployed artifact. A Sink whose Source hasn't been deployed yet simply waits — the dependency resolves the moment the Source appears. Teams deploy in whatever order suits them; there is no pipeline-wide release to sequence.

## Upgrades

The workflow is versioning-driven (see [Versioning](../concepts/versioning.md) for the model):

- **Patch / minor** — bump `version` in `pond.toml` and deploy. The new version replaces the old within its major line; consumers notice nothing but the improvement.
- **Major** — bump to the next major and deploy. This opens a *second concurrent line*: existing Sinks keep consuming the old major (which keeps executing), and each migrates by updating its own `[sources]` entry and deploying itself. Retire the old major once nothing depends on it.

A redeploy while the Pond is mid-run is safe: in-flight runs complete against the artifact they started with, and subsequent runs use the new selection.
