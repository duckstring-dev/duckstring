# Documentation & branding rewrite

Status: **proposed — branding section awaiting author sign-off.** This document holds the whole
rewrite: the repositioning, the brand frame, the audit of what exists, and the target structure.
Nothing below has been applied to `docs/`, the README, or the landing page yet.

## 1. What changed

The original framing sized Duckstring as a lightweight orchestrator for small-to-medium pipelines
(~100 GB), with DuckDB as a sensible *default* engine among swappable ones. The differentiator was
the package model; the compute story was deliberately modest.

Testing over the past months established that DuckDB is not merely the best local engine but the
fastest engine available, and that the sizing thesis behind it holds — one large instance beats a
fleet of small ones at equal cost. **This does not become Duckstring's pitch.** The speed belongs to
DuckDB and the credit should stay there. What it changes is the *size of the opportunity*: when the
industry works out that DuckDB is the right engine, a large number of teams will go looking for a
platform to run it on. Duckstring is positioned to be there.

The ecosystem now has three slots, and Duckstring claims the third:

- **DuckDB** — the engine.
- **MotherDuck** — the compute service.
- **Duckstring** — the platform.

So the relationship to DuckDB is structural and exclusive; engine pluggability is no longer a feature
to advertise. But the thing being sold is **organisation and opinionated structure**, not throughput.

### 1.1 The stance

The product is a *method* plus the tools that make the method the path of least resistance. Three
statements, in the author's framing:

- Not "supports incremental" — a thoroughly featured method for incremental work, built on best
  practices, that you use rather than assemble.
- Not "you can run pipelines" — a specific orchestration model that gives seamless upgrades and
  run-what-you-use.
- Not "you can modularise your transforms" — a thorough expectation that you do.

**Internal analogy (do not put in copy): Jira.** Plenty of work-management tools bolted themselves
onto whatever framework a team already had. Jira asked the industry to change how it worked and
handed it the tooling to do so — the methodology spread because the tool made it the easy path.
Duckstring is making the same move for data engineering. That is the ambition to hold the copy to;
naming Jira in public copy would be strange, so it stays here.

### 1.2 What actually persuades a business

A team does not begin a migration because something is fast. They begin one when they are confident
that the annoying parts of data engineering are handled for them, so they can inject business logic
and nothing else. That is the real pitch, and Duckstring can make it honestly today: orchestration,
incrementality, retries and failure states, versioning and upgrades, schema contracts, local testing,
deployment, lineage, alerting, metrics, egress, secrets, and data serving are all built.

That aggregate is currently invisible — it is spread across seventeen guides, and no page anywhere
states the total. Fixing that is one of the highest-value items in this rewrite (§3.4).

### 1.3 Speed: the reconciliation

The brief asked for minimal friction to "holy moly that's fast". That survives, but as an **experience
we arrange, not a claim we make**. A reader who runs their own SQL through Duckstring feels the speed
themselves within a minute, and attributes it — correctly — to DuckDB. We do not need to assert it,
and asserting it would put us in a benchmark argument we have no reason to be in.

So: **no speed claims, no numbers, no comparisons.** No benchmark page, no "20×", no 4h → 7s
anecdote, no fleet-versus-box thesis page. The competitor rule reverts to its original strict form:
no competitor is named in any copy, and there is now no benchmark carve-out because there is no
benchmark. Where the engine's performance comes up at all, one honest paragraph does it: DuckDB is
fast, that is DuckDB's achievement, and what Duckstring adds is making sure you only ever ask it for
the minimum and can keep asking as the project grows.

### 1.4 Rules retired

- "Duckstring is not a data engineering platform." — it is one, deliberately, and claims that slot.
- "The orchestrator is not the product."
- "Never describe Duckstring as an orchestration framework" — replaced: orchestration is one named
  component of the platform, described plainly. It is not the category and never leads.
- Mesh-pattern engineers as the *primary* audience — now one segment (§2.4).
- The defensive scale framing ("single-machine ≠ small data", no row cap). Compute sizing is a
  deployment detail in the cloud guide, not a positioning argument.
- DuckDB-as-default and engine pluggability as selling points.

### 1.5 Rules retained

- **The tagline is a closing beat, never the opener.** Lead with the descriptor, earn the tagline.
- **Never name a competitor in copy.** No carve-out.
- The README stays short; examples live in `docs/`; the author's opening paragraphs keep their voice.

## 2. Proposed brand frame

### 2.1 Tagline (decided)

> **Get your ducks in a row.**

Replaces "There is no DAG." Worth noting how well it now fits: the line is about *organisation*,
which is exactly what turned out to be the product. It is also inherently tabular ("in a row"), sits
in the duck ecosystem, and describes literally what the platform does — it puts the transforms in
order. Placement rule unchanged: it closes a surface, never opens one.

### 2.2 The opener

Descriptor sentence, on every surface:

> **The data engineering platform for DuckDB.**

Its job is to claim the slot, plainly and in one line. The opinion needs a full sentence and gets one
immediately after:

> Duckstring doesn't just let you modularise, version and incrementalise your transforms — it expects
> you to, and handles everything that follows.

And the line aimed at anyone considering a migration:

> **Bring the business logic. Duckstring handles the rest.**

I considered folding the opinion into the descriptor ("the *opinionated* data platform for DuckDB")
and recommend against it: "opinionated" is developer-register, reads as a hedge to a business reader,
and cannot carry the specific claim. Better to say the slot, then say the stance.

### 2.3 The four beats

Replacing the old package/DAG/demand triad. The first three are the method; the fourth is what makes
a migration thinkable.

| Beat | Claim | Built on |
|---|---|---|
| **Modularity isn't a discipline, it's the unit** | Every transform is a versioned package declaring what it consumes. You don't choose to modularise — the unit of work *is* the module. | Ponds, `pond.toml`, SemVer |
| **Incremental is a method, not a flag** | A complete, retraction-correct incremental engine you use, rather than a pattern you re-implement per table. | Trickle (DBSP / Z-sets), incremental I/O |
| **Execution is demanded, not scheduled** | Nothing runs unless something downstream asked; a breaking change ships without a coordinated release. | Freshness/demand model, concurrent majors, no-change skip |
| **The rest is handled** | Retries, failure states, contracts, lineage, alerting, metrics, egress, secrets, serving, local testing, deployment. | The whole Catchment surface |

The old package-model story is not discarded — it is *promoted* to the first beat and given a sharper
edge. Previously it was offered as a capability ("you can treat transforms as packages"); now it is
the stance ("this is how transforms work here").

### 2.4 Audience

1. **Teams migrating onto DuckDB from another platform.** They have chosen the engine, or are about
   to, and now face everything a platform used to do for them. Highest intent, and the group the
   "handled for you" material is written for.
2. **DuckDB users who have outgrown a script.** They already believe the engine claim; they need
   structure, and they are the ones most likely to adopt the method wholesale.
3. **Mesh-pattern teams hitting coordination walls.** The old primary audience, now a segment. The
   versioning and upgrade material is their entry point.

### 2.5 DuckDB, and the engine question

Say plainly that DuckDB is the engine — not a default, not one of several. Depth of integration is an
asset to advertise. Where other engines appear (the Flock dispatching over-envelope work), the
existing framing is already right and should be stated positively rather than buried: **DuckDB is the
authority.** Dispatch decides where work runs, never what is published; a foreign result is conformed
to DuckDB's answer or rejected.

**Open question for the author:** does *MotherDuck* get named in copy? It is a neighbour, not a
competitor, and the three-slot sentence is the clearest statement of the position available. But it
attaches Duckstring to another company's brand and invites questions about the relationship. My
inclination is to use the ecosystem framing in conversation, launch posts and this document, and keep
public copy to "DuckDB is the engine; Duckstring is the platform around it".

## 3. Documentation audit

`docs/` is a fully-written Docusaurus site (~6.5k lines over 39 files) plus a hand-built landing page.
It is in better shape for this positioning than it was for the speed-led one I proposed first — much
of it is already method content. The problems are framing, ordering and omission, not quality.

### 3.1 Critical

| Surface | Problem |
|---|---|
| **Vocabulary before result** | The single biggest friction. `docs/docs/index.md` introduces Pond, Ripple, Trickle, Catchment, Source/Sink, Inlet and Outlet — seven terms in a table — before the reader has seen anything run. The quickstart then adds Catchment setup, deploy, and four demo Ponds before a line of the reader's own SQL executes. The first hurdle is *"how do I run my SQL on this"*, and nothing in the current path answers it inside five minutes. |
| **The demo is the front door** | `duckstring pond demo` builds four Ponds whose Ripples *sleep* — `join_lines` is a deliberate 3-second bottleneck — and the quickstart celebrates settling into a 3-second cadence. It is an excellent teaching device for the orchestration model, which is now a mid-funnel reveal rather than the opening pitch. It should move to where that model is taught. |
| `docs/src/pages/index.tsx` | Hero is "There is no DAG." The positioning comment block at the top encodes the retired rules. The incremental section apologises for single-node ("perfect for the 90% of cases where you don't *actually* need distributed compute") — defensive, and about compute, which is no longer our subject. No statement of the ecosystem slot, no DuckDB relationship, no "handled for you". |
| `docs/docs/index.md` | Opens on coordination and ownership — the mesh pitch, aimed at what is now the third audience. Needs a new first problem (§4.2), the DuckDB slot, and the vocabulary table deferred out of the intro. Closes on the retired tagline. |
| `README.md` | Closes on the retired tagline. Concept list leads with the Catchment. No statement of the stance or the handled-for-you total. Author's opening paragraphs keep their voice; the frame around them changes. |
| `docs/docusaurus.config.ts` | `tagline: 'There is no DAG.'` |

### 3.2 Significant — reframe, content sound

| Surface | Change |
|---|---|
| `getting-started/quickstart.md` | Rebuilt around the reader's own SQL (§4.3), not the sleep demo. |
| `getting-started/playground.md` | Currently first in the sidebar and billed as "the fastest way to understand Duckstring". It simulates orchestration — a mid-funnel reveal now. Demote to Concepts; do not delete. |
| `getting-started/installation.md` | Describes DuckDB parenthetically as "the embedded analytical database every Pond computes against". State the relationship directly instead. |
| `concepts/*.md` | Each page describes its noun accurately but neutrally. Each should open with the *stance* for that noun — why it works this way, what it refuses to let you do — before the mechanics. This is where the opinion actually gets transmitted. |
| `concepts/trickle.md`, `guides/trickle.md` | Trickle rises from "a Ripple variant" to the second beat. Content is good; framing and placement change. |
| `guides/cloud.md` | 292 lines, well written. Contains the sizing material, which stays exactly where it is — a deployment detail, not a headline. |
| `theory.md` (1467 lines) | The spec is authoritative; do not touch it. Add a short preface framing it as the mechanics behind "demanded, not scheduled", so a reader arriving mid-funnel knows why they are reading about Kanban. |
| `incremental-theory.md` | Good as is. It is the rigour behind beat two — the evidence that "a method, not a flag" is a real claim. |

### 3.3 Fine as-is

`concepts/{ponds,ripples,freshness,versioning,puddles}.md` mechanics, all of `reference/`, and the
operational guides (`triggers`, `windows`, `control`, `fault-tolerance`, `deploying`, `local-testing`,
`dbt`, `lineage`, `querying-data`, `web-ui`, `connecting-catchments`, `external-pipelines`,
`running-a-catchment`). Accurate descriptions of a real surface. They need re-grouping (§4.4), a sweep
for retired copy, and the stance-first opening where they carry a concept.

### 3.4 Missing

1. **"What you don't have to build"** — the aggregate of handled concerns, on one page. The single
   most valuable addition for audience #1, and it requires no new engineering: everything on the list
   already ships. Today a reader can only discover it by reading seventeen guides.
2. **"The Duckstring way"** — the stance stated once, plainly: why every transform is a package, why
   incremental is the default rather than an optimisation, why nothing runs on a schedule. The page
   that asks the reader to change how they work, and says what they get for it.
3. **"Migrating an existing pipeline"** — high-intent, and the shape is already settled: step 1, the
   existing project becomes one Pond with many Ripples (SQL unchanged, gains versioning and declared
   dependencies); step 2, split along ownership lines when it earns it. No numbers, no comparisons.
4. **"When not to use it"** — genuinely multi-node work, and anything else we would lose. Cheap, and
   it buys credibility with the reader who is being asked to change how they work.

Dropped from the previous draft of this plan: benchmarks, the one-machine thesis page, and sizing as a
headline. All three were speed-led and none survives §1.3.

## 4. Proposed structure

### 4.1 The funnel

Four steps, each answering the objection the previous one raises:

1. **What is this** — the slot, the stance, and what it handles for you. (Landing / intro.)
2. **Run my SQL** — under five minutes, minimum vocabulary. They feel the engine here; we claim
   nothing.
3. **What did I just get** — the reveal. The special parts, demonstrated after the fact rather than
   explained as prerequisites.
4. **The method** — concepts and guides, for a reader who has now seen it work.

The current site does 4 → 1 → 2 and never really does 3.

### 4.2 The new opening problem

The intro's current problem statement (coordination, ownership) is the mesh pitch. Under the new
audience ordering the opening problem is different, and better:

> You've chosen the engine. A platform is more than an engine — it's orchestration, incrementality,
> versioning, deployment, testing, observability, and getting data back out again. Built in-house
> that's a year of work and usually a worse version of what you left. Duckstring is that layer, and
> it has opinions about how you should use it.

Coordination and ownership survive as the *second* problem, feeding beats one and three. The existing
prose on both is good and mostly transfers.

### 4.3 The new quickstart — "run your SQL"

The subtlety the author named: be clear what's special without turning people off. The failure mode is
opening with "wrap your SQL in our decorator and learn seven nouns" — the reader bounces before
reaching anything that would have convinced them. The fix is to **defer the vocabulary and reveal the
specialness as a consequence.**

Target shape, ~5 minutes, two nouns (Pond, Ripple) and no others:

1. **Your SQL runs, unchanged.** A file, a decorator, `duckstring pond run` locally. No Catchment, no
   deploy, no triggers. They see their own data come out, at DuckDB speed, without us mentioning
   speed.
2. **Add a second transform that reads the first.** Declare the dependency; the ordering just happens.
   No DAG was written. (Beat one, demonstrated, not explained.)
3. **Run it again with nothing changed.** Nothing recomputes. (Beat three, demonstrated in one
   keystroke.)
4. **Then, and only then:** deploy it to a Catchment, and link out.

Steps 2 and 3 are the whole pitch, delivered as things the reader watches happen rather than claims
they are asked to accept. `duckstring pond run` and Puddles already support step 1 locally, which
means the quickstart can defer the Catchment entirely — a significant friction win that the current
path doesn't take.

The sleep-based `--ripple` demo **stays** and becomes the centrepiece of the orchestration material,
where its artificial durations are a teaching device rather than a first impression. The real-data
demos (`--gharchive`, `--tpcds`) become the incremental-at-scale material.

### 4.4 Target sidebar

```
Introduction                     ← the slot, the stance, what's handled for you

Getting Started
  Installation
  Quickstart                     ← "run your SQL" (§4.3)
  What you just got              ← NEW: the reveal, after the fact
  Deploying it                   ← the Catchment, once they want one

The Duckstring way               ← NEW: the stance, stated once
What you don't have to build     ← NEW: the aggregate of handled concerns
When not to use it               ← NEW

Concepts                         ← each page opens with its stance, then mechanics
  Ponds · Ripples · Trickle · Freshness & demand · Versioning · Catchment
  Puddles · Playground           ← Playground relocated here

Guides
  Build:    creating-a-pond · local-testing · incremental-ripples · trickle · dbt
  Migrate:  migrating an existing pipeline (NEW) · external-pipelines
  Run:      deploying · triggers · windows · control · fault-tolerance
  Operate:  running-a-catchment · web-ui · querying-data · lineage · cloud
  Connect:  connecting-catchments

Theory                           ← + preface tying it to "demanded, not scheduled"
Incremental Theory

Reference                        (unchanged)
```

Three top-level pages sit between Getting Started and Concepts, which is unusual but deliberate: they
are the pages that convert a reader who has just seen it work, and burying them inside Concepts would
put the stance behind the vocabulary again. The alternative is folding them into a "Why Duckstring"
category; I prefer them flat, because each is a single page a reader might be linked to directly.

### 4.5 Landing page

Rewrite of `docs/src/pages/index.tsx`, keeping the dark-canvas styling and the `DemoSlot` video
machinery. More of the existing page survives than under the previous draft — the throttle, upgrade
and on-ramp sections are all method content and are good.

- **Hero** — descriptor, the stance sentence, `pip install duckstring`. Not the tagline.
- **The slot** — DuckDB is the engine; this is the platform around it. Short.
- **Modularity is the unit** — the existing `#upgrade` section, promoted, reframed from "you can" to
  "this is how it works here".
- **Incremental is a method** — the existing `#incremental` section, with the apologetic single-node
  paragraph cut and the DBSP rigour put in its place.
- **Demanded, not scheduled** — the existing `#demand` throttle section, largely intact. It is the
  strongest thing on the page.
- **Bring the business logic** — NEW, and the closer that matters for a migration: the handled-for-you
  list, made concrete.
- **Drop it into what you run** — the existing `#start` on-ramp, intact.
- **Close** — *Get your ducks in a row.*

The positioning comment block at the top of the file must be rewritten alongside it; it is what keeps
the page honest across future edits.

## 5. The demo problem

The tension as posed: showing speed wants tens of GB, which is a rough ask of someone just fiddling;
showing the orchestration model wants visible execution time per Pond, which at MB scale is
milliseconds; and adding sleeps to make it visible works against the thesis. Below is the case that
the trade-off is softer than it looks, and that the part of it that is real dissolves under a better
framing.

### 5.1 Decompose what a demo has to show

Six distinct claims are currently collapsed into one artefact. They have very different requirements:

| Claim | Needs duration? | Needs scale? |
|---|---|---|
| My SQL runs here | no | no |
| Dependency ordering just happens | no | no |
| Nothing re-runs when nothing changed | **no — better when instant** | no |
| Only the delta is processed | contrast only | contrast only |
| Bottleneck throttling, demand propagation, overlapping runs | **yes** | no |
| It is fast | no | **yes** |

Only *one* row genuinely needs wall-clock duration and only *one* needs scale — and they are different
rows. Trying to satisfy both in a single demo is what manufactures the conflict. Nothing else on the
list is harmed by being instant, and the no-change skip is actively *better* when instant: "nothing
ran" is a stronger punchline at 40 ms than at 3 s.

### 5.2 Scale is cheaper than it looks — the cost is download, not size

The `--trickle` demo already generates its 1.5M-row history in SQL (`FROM range(n)` in
`demo/orders/src/pond.py`), sized by `DUCKSTRING_DEMO_ORDERS`. Nothing is downloaded and nothing is
shipped. DuckDB generates rows faster than a reader could download them, so the scale dial costs
generation time and disk — not bandwidth, and not a fiddly setup step.

That reframes the ask. "Download tens of GB" is indeed unreasonable; "turn a dial and wait ten seconds
while your own machine makes 50M rows" is not. The existing demo already has this dial and the docs
never mention it. A `--scale` flag on `pond demo` that sets those env vars would make it a one-word
choice, and the heavy demo becomes opt-in rather than the default burden.

Local generation also beats a download for the speed story specifically, because the reader watches
the data *appear* and then watches it be queried — the volume is established in front of them rather
than asserted in a README.

### 5.3 The problem with the sleeps is the label, not the sleeps

The `--ripple` demo's sleeps are a legitimate teaching device. What damages the thesis is the framing
around them. The quickstart currently says:

> With `join_lines` at 3 seconds, every Pond settles into a ~3-second cadence.

A reader with no other information concludes that a ~3-second cadence is what this tool does. The
sleeps are never presented as stand-ins for anything; they read as the product's speed. That is the
actual harm, and it is fixable in prose.

Note the codebase already has the right instinct and states it in comments: `orders` and `revenue`
carry explicit notes that they contain *no* `time.sleep` because "the work here is the data, not a
stub". The `--trickle` set is honest about its timings; the `--ripple` set is the one that isn't
labelled.

### 5.4 Slow does not mean big

The deeper move. Real pipelines are routinely slow for reasons that have nothing to do with data
volume: a vendor API, an hourly file fetch, a partner system, a model that takes four minutes, another
team's job you have to wait on. A demo whose bottleneck is one of *those* is both honestly slow and
more representative than one whose bottleneck is a big table.

This matters because it changes what the sleep is. A `time.sleep(3)` documented as "stands in for a
remote export that takes three seconds" is an accurate model of an extremely common situation, and it
teaches the proxy-Pond pattern that `guides/external-pipelines.md` already documents. A bare
`time.sleep(3)` in a demo billed as a normal pipeline is a fiction. Same code, different contract with
the reader.

### 5.5 The reframe that dissolves the tension

The two theses only fight while orchestration is being justified by *slowness*. Invert it:

> When compute stops being the bottleneck, organisation becomes the bottleneck.

A fast engine makes the orchestration model **more** valuable, not less, for three reasons worth
stating plainly in the docs:

- **What remains slow isn't the transform.** It's the fetches, the external systems, the waiting. The
  engine removed its own share of the latency and left everything else standing.
- **Cheap runs hide waste rather than removing it.** When a job takes four hours you schedule it
  nightly and accept the staleness; when it takes three seconds you can run it continuously — and then
  nothing stands between you and paying for results nobody reads except a system that knows when *not*
  to run. Fast compute makes waste invisible, not absent.
- **The constraint moves to the humans.** Coordination, upgrades, knowing what depends on what. Beats
  one and three exist for exactly that, and they get *more* important once the engine is quick.

So the demos should not be apologising for showing duration. They should be showing where the duration
actually lives in a real pipeline — which is not in DuckDB.

### 5.6 Instrument, don't inflate

The general fix for "too fast to see", and the most valuable feature suggestion in this document.

If a run is too quick to watch, print what it *did*. Duckstring already knows: which Ponds ran, which
were skipped by the no-change gate, the standing row count, the delta size, elapsed per Ripple. Today
`pond run` prints only per-Ripple durations, and `ripple_run` records timings but not volumes.

A summary line turns the absence of duration into the punchline:

```
4 Ponds · 1 ran · 3 skipped (no change)
4,218,004 rows standing · 25,000 changed · 0.6% recomputed
elapsed 71ms
```

That is the product's entire claim, stated as a measurement of the reader's own run, with no
comparison to anyone and no number we assert. It reads identically well at 50 MB and at 50 GB, which
means **the demo stops needing to be big at all** — the contrast is carried by the counters rather
than by the clock. It is also genuinely useful in production, and it feeds the web UI and run history.

Cost: `ripple_run` needs rows-read/rows-written columns (a migration), the executor and Trickle need
to report them (Trickle already computes delta sizes), and the CLI needs the summary. Small, and it
pays off in every demo, the quickstart, the UI, and the docs simultaneously.

### 5.7 Proposed demo ladder

Mostly relabelling and re-sizing what exists, plus the instrumentation above.

| Tier | Command | Shows | Cost to the reader |
|---|---|---|---|
| 0 | quickstart, no demo | your SQL runs; ordering happens; nothing re-runs | seconds |
| 1 | `pond demo` | structure, dependency, no-change skip | ~1s, MB |
| 2 | `pond demo --incremental` (today's `--trickle`) | full build vs. delta, by counters and clock | ~10s generated |
| 3 | `pond demo --incremental --scale` | the same, at a size where the clock speaks for itself | ~1 min generated |
| 4 | `pond demo --orchestration` (today's `--ripple`) | throttling, demand propagation, overlapping runs | seconds, simulated |
| 5 | `pond demo --gharchive` | a real pipeline: real data, real network latency, real deltas | network |
| 6 | `pond demo --tpcds` | a recognisable star schema at scale | generated |

Tier 4 keeps its sleeps and gains an honest frame: it is a pipeline whose bottleneck is *external
work*, each sleeping Ripple documented as the thing it stands for. That is the case where
demand-driven execution saves the most, so the demo argues for the model instead of against the
engine.

The playground deserves a note here too. It is the one surface where simulated durations are
unimpeachable, because it is openly a simulation — which is a real argument for keeping it as the home
of the orchestration model rather than retiring it.

## 6. Feature suggestions

Three, in order of value to the rewrite. All are optional; the docs work without them, but each
removes a friction the docs would otherwise have to write around. **Planned in detail in
`plans/repositioning-features.md`** — summarised here for the reader of this document.

### 6.1 Run instrumentation (recommended)

Per §5.6. The highest-leverage item in this document, because it makes every demo work at every size
and it converts the product's central claim into something the reader measures rather than reads.

### 6.2 Native `.sql` Ripples (recommended)

The quickstart's first step still asks the reader to put their SQL inside a decorated Python function.
For the migration audience — who arrive holding a folder of SQL — that is the first hurdle, and it is
avoidable.

dbt-mode is the existing answer and it is a good one, but it requires adopting dbt. A native mode — a
directory of `.sql` files, one per output table, dependencies taken from references or a small
frontmatter block — would let the on-ramp be "point Duckstring at your SQL folder".

The machinery for this largely exists. `dbt_mode.manifest_to_ripples` already translates a foreign
model graph into the standard ripple-row shape (`{"func", "name", "parents", "always_run"}`), which
`routes/deploy._register` and `duck/executor.load_topology` consume unchanged. A native SQL translator
plugs into the same seam, and `DbtExecutor` is the template for the executor side. This is a modest
feature with a disproportionate funnel effect.

### 6.3 MotherDuck integration (worth scoping)

If the three-slot positioning is the story, being a good citizen of that ecosystem is worth something
concrete. Three levels, in increasing cost:

- **As an egress target (`md://`)** — a Pond publishes straight into MotherDuck, where the
  organisation's BI tools already look. This is a **Spout driver** and the seam already exists
  (`egress/base.get_egress`, alongside `file`/`s3`/`postgres`). Cheapest, most natural, and it makes
  the ecosystem story real rather than rhetorical.
- **As a Source** — read MotherDuck tables into a Pond. Also small: `ATTACH 'md:…'` and the existing
  foreign-read path.
- **As compute** — run Ripples on MotherDuck rather than a local Duck. Much larger: it touches the
  registry, the executor, and the whole local-disk assumption. Structurally this is a Flock-shaped
  problem (dispatch elsewhere, conform the answer), not a new execution model. Not the first move.

Recommendation: egress driver first, Source second, then a **MotherDuck Flock engine** — which is
less an integration than the best version of a feature that already exists. The Flock's allow-lists
and shape restrictions exist entirely because Trino is not DuckDB; MotherDuck is, so `eligible()`
opens up to the whole builder surface and the dialect compiler disappears. See
`plans/repositioning-features.md` §3 for all five layers, including the version-skew risk that
replaces the dialect one.

## 7. Sequencing

1. Sign off §2. Everything downstream depends on it.
2. The quickstart (§4.3) — verify the local `pond run` path really is a five-minute, two-noun
   experience before committing the narrative to it. This is the load-bearing item now.
3. The three new stance pages — "The Duckstring way", "What you don't have to build", "When not to
   use it". Writing only; no new engineering.
4. Landing page, README, intro — the three surfaces that must move together.
5. Structural moves: sidebar, guide grouping, playground and demo relocation.
6. Sweep for retired copy: "There is no DAG", the apologetic single-node framing, DuckDB-as-default,
   Catchment-led concept lists.
7. Stance-first openings across `concepts/`.
8. Migration guide.

Feature work (§6) is independent of the writing and can proceed in parallel. Run instrumentation is
the one that would change what the quickstart and demo pages say, so it is worth deciding on early.

## 8. Open questions

- **Naming MotherDuck in copy** (§2.5). Ecosystem clarity versus attaching to another brand.
- **How much of the stance can be asserted before it reads as arrogant.** The Jira move requires
  telling people their current approach is wrong. Where is the line for this audience?
- **Does the playground survive?** It teaches the demand model better than prose does, but it is a
  maintained surface with its own domain, and it now sits mid-funnel rather than at the top.
- **Does "run your SQL" mean a decorator?** §6.2 proposes native `.sql` Ripples to remove this. If
  that lands, the quickstart changes shape; if it doesn't, the quickstart has to make a decorated
  function feel trivial rather than ceremonial.
- **Is the demo default tier 1 or tier 2?** Tier 1 is instant and teaches structure; tier 2 is the
  one people will screenshot. With instrumentation (§5.6) tier 1 may be enough.
