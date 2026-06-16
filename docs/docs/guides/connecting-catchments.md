---
title: Connecting Catchments
description: Draw a Pond from another Catchment — the package graph spanning teams and machines.
---

# Connecting Catchments

The package graph doesn't have to live on one Catchment. A team that owns a group of Ponds can publish them as data products; another team consumes them without sharing a runtime. And a Pond that needs heavier compute can run on its own Catchment, drawn into the pipeline that drives it.

The mechanism is a **duct**: a one-directional conduit that draws a Pond from an upstream Catchment into yours. The drawn Pond appears locally as a **Pond Draw** — a real node that copies the upstream's published data into your Catchment, so the Ponds depending on it consume it exactly like any local Source. Demand flows *up* the duct; data flows *down*.

## Drawing a Pond

Both Catchments must be registered with the CLI (the consumer needs to reach the upstream — see [Running a Catchment](running-a-catchment.md)):

```bash
duckstring catchment connect --name products-cat --path https://products.host:7474 --key …
duckstring catchment connect --name main --path http://127.0.0.1:7474
```

Open a duct on the **consuming** Catchment, pointing at the upstream, then choose which of its Ponds to draw:

```bash
duckstring catchment duct create products-cat -c main   # conduit from products-cat into main
duckstring catchment duct add products-cat products -c main   # draw the `products` Pond
```

`duct create` records the upstream's address, credentials, and [stable identity](#identity-and-the-lineage-view) on the consuming Catchment. `duct add` materialises the Pond Draw — from that moment a local Pond whose `pond.toml` names `products` as a Source wires straight to it, no code change. Use `duct sync` instead of `add` to draw *every* Pond the upstream currently exposes (`duct create --sync` does both at once), and `duct ls` to see your ducts and what they draw.

That's all the consumer side needs. The upstream requires nothing special for a duct — it serves its already-published data and accepts the demand the duct forwards, gated by its [API key](running-a-catchment.md#authentication).

## How a Draw behaves

A Pond Draw is a first-class local node, drawn dashed and prefixed `[DRAW]` in the [web UI](web-ui.md). It sits between the upstream Pond and your local consumers:

- **Demand flows up.** When a local Pond pulls or is pushed to a freshness the Draw can't yet satisfy, the duct forwards that demand to the upstream — a [Tap or Pulse](triggers.md) carrying the same freshness — so the upstream produces.
- **Data flows down.** When the upstream is fresh enough, the Draw transfers its published Parquet into your Catchment and advances — it shows *running* while copying, *idle* when done.
- **Freshness stays coherent.** A Pulse at time `T` yields freshness `T` for every node it reaches, on either side of the duct — so a drawn Pond and a local Inlet pulsed together land at the same freshness, with no spurious "newer" edge between them.
- **Faults propagate.** If the upstream Pond fails, is killed, or becomes unreachable, the Draw is [blocked](fault-tolerance.md) — and so is everything downstream of it, exactly as a local failed Source would block its Sinks. It recovers when the upstream does.

Transfers happen the moment the upstream is fresh (the duct holds a long poll, not a timer), so a drawn pipeline is about as snappy as the upstream's own run time allows.

## Identity and the lineage view

Every Catchment mints a **stable identity** (a UUID) on first start, shown in the top-left box of its web UI alongside its name. Ducts record the upstream's identity, which is how the UI resolves the full picture.

The canvas shows not just your Ponds but the **entire upstream lineage**, recursing up the ducts: each upstream Catchment is drawn as a labelled container holding the Ponds that feed yours, with the duct edges crossing into your Draw nodes. You can trace a stalled local Pond all the way back to the Catchment — and the Pond — responsible, even several hops away. A Catchment that appears on both ends of a chain (you draw from it and it draws from you) renders as the real round-trip rather than looping forever.

## Exposing a Pond for reads

Separately from ducts, you can mark a Pond **open** so a non-Catchment consumer — a dashboard, say — can read it and have a read lazily refresh it:

```bash
duckstring catchment open reports --tap-on-get   # a data read fires a Tap; the snapshot is served first
duckstring catchment close reports
```

`--tap-on-get` only affects the [query API](querying-data.md): a read returns the current snapshot immediately and triggers a Tap behind it, so the next read is fresher. (Ducts manage their own demand and ignore tap-on-get.)

## What to know

- **A duct is a full-trust link.** It holds the upstream's credentials, which under the built-in API key grant full access. So today's ducts are for Catchments within one trust domain — your own machines, or a team you'd hand a key. Scoped per-link credentials are a future step; until then, don't duct in a Catchment you wouldn't share a key with.
- **Transfers copy the whole table.** Each refresh pulls the upstream Pond's full exported Parquet (no incremental delta yet). At Duckstring's single-node scale that's seconds; the `--incremental` flag on `duct add` is reserved for when delta transfer lands.
- **Names share a namespace.** A drawn Pond can't share a `name@major` with a local Pond — remove or rename one. Cross-Catchment dependency cycles aren't detected centrally (no Catchment sees the whole mesh); the freshness model doesn't deadlock on them, but they're yours to avoid.
