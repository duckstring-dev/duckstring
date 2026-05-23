# Brand Strategy (WIP)

## What Duckstring is

A packaging standard for data transforms. Not an orchestration framework. The distinction matters because "orchestration framework" puts it in a crowded category where the comparison is feature lists against established tools. "Packaging standard" is a category Duckstring can own, and it's the accurate description of the actual differentiator.

The central idea: data transforms have the same dependency structure as software packages. If you version each transform and let it declare its parents — the same way `pyproject.toml` declares dependencies — the execution DAG is implicit in the package graph. You don't build it. You don't govern it.

The Catchment is the reference runtime that executes Ponds. It is a convenience. The value of the Pond packaging model exists independent of what executes the Ponds. Never lead with the Catchment.

## Target audience

**Primary**: Data engineers who have hit the coordination and ownership walls of large transform pipelines. The specific pain:

- A schema change in one transform forces everyone downstream to coordinate simultaneously — migration windows, deprecation notices, synchronised deploys
- Ownership is fuzzy because nothing structural prevents any transform depending on any other
- Breaking changes are a social and organisational problem, not a technical one

These engineers have typically already tried to impose structure through conventions, folder organisation, or access controls. The most mature version of this is a mesh pattern — splitting transforms into separately managed projects with designated public interfaces.

**Warmest segment**: Engineers who have adopted or seriously considered a mesh pattern and found it still requires organisation-wide coordination for breaking changes. They've already reasoned their way to needing package-like boundaries. They're missing SemVer, concurrent version execution, and automatic retirement. Duckstring gives them exactly those things.

Do not target: teams with simple pipelines who haven't hit these problems yet, data scientists who don't think in terms of pipeline ownership, teams that primarily care about big-data scale (Duckstring is designed for single-node workloads, ~<50M rows).

## Framing for mesh-pattern users

The pitch is not "replace your existing tools." It is: "the coordination problem you still have after adopting a mesh pattern is what Ponds solve."

Key concepts that map cleanly from their existing mental model:
- Project with a public interface → Pond with a SemVer
- Depending on another project's model → declaring a source in `pond.toml`
- Breaking change requiring org coordination → major version bump; downstream upgrades on own schedule
- Deprecated version kept alive during migration → old major version running concurrently, retiring automatically when nothing depends on it

What's new (and better):
- SemVer is enforced by the framework, not convention
- Multiple major versions running concurrently is a first-class feature, not an operational burden
- Version retirement is automatic — no one has to manage the deprecation window
- The dependency graph is explicit in code, not inferred from documentation

## Migration path from a mesh-pattern project

**Step 1**: Migrate to a single Pond. The entire existing project becomes one Pond with many Ripples. Immediate gains: SemVer versioning, declared dependency on upstream Ponds, participation in the Catchment execution model. Loss: no independent deployment per transform within the Pond.

**Step 2** (deliberate, not automatic): Refactor into multiple Ponds along logical ownership lines. This is an architectural decision that requires human judgment — there is no automated split. Generally: one team, one Pond. The Pond boundary is the team boundary.

There is no Step 0.5 where everything works automatically. Step 1 requires structuring transforms as Ripples and writing `pond.toml`. It's not a one-command migration but it's not a rewrite either — the SQL/logic stays the same.

## Key gap: incremental transforms

Many mesh-pattern engineers rely heavily on incremental processing — transforms that only process new rows rather than full refreshes. This maps to Duckstring's **Trickle** concept, which is not yet hardened.

The design intent: Trickle exposes the watermark to the transform function, so the full/incremental branch is written once and the framework handles when each applies. This should make incremental dbt models straightforward to migrate. Until Trickle is production-ready, incremental-heavy projects are a poor fit.

Do not downplay this gap in positioning. Engineers will find it immediately and it's better to have named it.

## What the Catchment is and isn't

The Catchment holds runtime state that can't live in the packages themselves: which major versions are active, demand records, watermarks, generation counts. This state has to live somewhere when you're executing — but it doesn't need to exist at all until execution is required.

A team could adopt the Pond packaging model today, run their Ponds with an existing orchestrator, and get the versioning and ownership benefits without the Catchment. The Catchment becomes relevant when they want the demand/watermark orchestration model (pull-based, optimal pipelining, automatic version retirement).

Don't position the Catchment as competing with Airflow/Prefect/Dagster. The right framing: "bring your own executor and you still get versioned transforms and clean team boundaries; the Catchment is there when you want the full stack."

## Distribution

No social media presence. Viable channels in rough priority order:

1. **Show HN** — the best single channel for developer tools with a novel premise. New accounts are fine; the content is the signal. Tuesday–Thursday mornings US Eastern. Post should be two sentences, no hype. Let the comments be the pitch.

2. **A post arguing the central idea** — not "I built a thing" but an argument: why data transforms should be versioned like packages. Written before or alongside launch, hosted on a personal domain or dev.to. This gets found through search and shared independently of the product.

3. **Data engineering communities** — r/dataengineering, relevant Slack communities. Requires either existing karma/presence or a trusted proxy for the initial post. Better approach: genuinely answer questions where the Pond model is relevant, mention Duckstring naturally.

4. **The GitHub repo itself** — well-set topics, working demo, good README. Organic discovery from search.

Don't launch before the demo runs cleanly end-to-end. "Most features not yet implemented" is honest in the README but is a launch-blocker for any public post.

## Copy decisions

**Tagline**: "There is no DAG."
- The DAG exists but is implicit. The point is that you don't govern it.
- Matrix reference is intentional — the audience is technical enough to have it in their meme vocabulary, and the philosophical reading (free your mind from the construct) earns its place.
- Works as a provocation for someone who knows what a DAG is. For anyone else it's confusing, which is fine — they're not the audience.

**Package description**: "A packaging standard for data transforms." (one line, no hedging)

**README opening**: See `copy.md` for the current version. User is rewriting manually from the draft — do not overwrite.

Key principle for all copy: write for the frustrated engineer who already has the pain. Don't explain the pain to people who don't have it yet; they're not ready. Every sentence should either sharpen the problem, explain the mechanism, or signal what's real and what's not yet built.

Do not mention competitors by name anywhere. The framing should stand on its own over time.
