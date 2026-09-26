# Documentation Style Guide

This guide is for anyone writing or rewriting pages in `docs/`, human or AI. It records the preferences worked out while reviewing sample rewrites of the Duckstring docs.

## Read this first

**Clarity for the intended reader is the goal.** Everything else in this guide exists to serve that goal, and most of it is a default, not a law.

The rules below come in two strengths:

- **Firm**: follow them unless the author says otherwise. They're marked as firm where they appear, and collected in [Firm rules](#firm-rules).
- **Soft**: everything else. These are tendencies to lean towards, not boxes to tick. When following one would make a passage harder to read, more awkward, or less accurate, break it.

A note for AI writers in particular: the most common failure is not ignoring this guide but applying it too literally. Some examples of over-compliance:

- Rewriting every sentence that contains a colon because colons are "discouraged".
- Turning every paragraph into bullets because bullets are "preferred".
- Stripping all technical vocabulary until the page reads as if it's written for people who've never worked with data.
- Adding a problem statement to a four-line section that doesn't need one.

If you notice yourself contorting a sentence to satisfy a rule, stop and write the clear version instead. A page that reads naturally and breaks two soft rules is better than a stilted page that follows all of them.

## Audience

The reader is a working data engineer. They know SQL, joins, primary keys, CDC, SCDs, batch versus streaming, warehouses, and roughly what a DAG is. They do not yet know Duckstring's own vocabulary (Pond, Ripple, Trickle, Catchment, freshness, demand, and so on) until a page has introduced it, or until an earlier page in the sidebar has.

This has two consequences that pull in opposite directions, and both matter:

- **Don't write for lay people.** Plain words are good, but explaining what a table is, or over-simplifying the mechanism, reads as patronising. An early rewrite was judged "a little too simple-English" for exactly this reason. Aim for professional and concise.
- **Don't assume Duckstring knowledge.** Every Duckstring-specific term must be introduced before it's relied on, either on the page or on a page the reader has clearly already read. Don't bring in concepts a newcomer wouldn't know yet (for example, mentioning ducts or cross-Catchment draws on a core concepts page).

Specialised vocabulary from outside Duckstring is fine when the audience knows it and it makes the text shorter. It's not fine when it adds a second jargon layer on top of Duckstring's own. For example, "backpressure" is standard in streaming but was judged unnecessary in the freshness page, because plain prose explained the same idea without adding a term. Use complex wording only when it buys brevity.

## Voice and register

- Professional, concise and natural. Write as a knowledgeable colleague explaining something, not as a textbook or a marketing page.
- Let ideas flow into one another. Sentences should connect; each paragraph should lead to the next. An overly compressed rewrite was judged "jerky": correct, but a sequence of dense statements with no connecting tissue. Density of Duckstring terminology made it worse.
- Don't overcorrect towards brevity at the expense of flow, or towards flow at the expense of brevity. The target sits between the two.
- Prefer ordinary words and constructions. Avoid unusual expressions, idioms, and phrases that draw attention to themselves.
- Use "you" for the reader. Use "Duckstring" (not "we") for what the system does.
- Motivation sections in particular tend to run long. State the problem and why it matters in a few sentences, then move on.

## Page structure

Readers need to understand why they'd bother with something before they can absorb how it works. Where a page or section introduces a concept or feature, order it like this:

1. **The problem.** What goes wrong, or what's costly, without this.
2. **The need.** What a solution would have to do.
3. **The concept.** Name the Duckstring feature that meets that need.
4. **The mechanism.** How it works, in as much detail as the page type calls for.

This applies at two levels: the page as a whole, and each major section within it. It can be very concise, often one or two sentences each for the first two steps.

This is soft. Skip or shorten it where it doesn't fit:

- Reference sections (a table of parameters, a list of CLI flags) don't need a motivation.
- A section that follows naturally from the previous one may not need its own problem statement.
- Don't invent a problem that nobody has, just to have one.

When a page previously ended with a "why this is better" section, consider moving that argument to the top as the page's motivating problem, rather than repeating it at both ends.

## Page types

Keep different kinds of documentation on different pages. Mixing them was the main structural problem with the old Trickle guide.

| Page type | Purpose | Code | Typical shape |
|---|---|---|---|
| Concept | explain the model and the reasoning behind it | little or none | problem-led sections, tables, diagrams |
| Guide | show how to do something well, with practical advice | plenty, in worked examples | task-led sections, short motivation, examples from the demo Ponds |
| Reference | exhaustive, precise description of an API or CLI surface | signatures and small snippets | predictable, scannable, minimal motivation |

- A concept page should make sense without reading any code. Where it helps, a small table of example rows is often clearer than a snippet.
- A guide can link to the concept page instead of re-explaining the model.
- Reference material should be complete and exact; it doesn't need narrative.
- `theory.md` and `incremental-theory.md` are specs with their own register (mathematical, precise). This guide applies to them only loosely.

## Order of concepts

Introduce ideas in the order the reader needs them:

- Put the foundational idea first, even if it isn't the headline feature. For Trickles, the change format (weighted rows) came first because every later section depends on it.
- Move from what a reader encounters first to what they encounter later: storage shape, then reading changes, then joins, then aggregation, then operational concerns.
- Defer implementation details (internal column names, storage tiers, strategy flags, fast paths) to guides or reference pages unless they explain something the reader needs at this level. A detail that explains the cost model (such as a default threshold) may earn its place on a concept page.

## Formatting

Mix prose with structure. Prose everywhere is hard to scan; structure everywhere reads like notes.

- **Tables** suit comparisons (append versus merge), mappings (change type to rows recorded), and "which case applies" summaries (source type, consumer type, behaviour).
- **Bullets** suit genuine lists: sets of conditions, properties, or steps without ordering.
- **Numbered lists** suit ordered steps (the stages of a join maintenance algorithm).
- **Mermaid diagrams** suit flows and structure: data moving between tables, what's stored where, a time window over a changelog. Use them where they show something prose can't show as quickly, not as decoration. The site already renders mermaid (see `index.md`). Keep labels short, use `<br/>` for line breaks, and check they render with `npm start`.
- **Prose** suits explanation, motivation and anything with a chain of reasoning.

Headers:

- Headers are plain labels: "Append and merge", "Reading a window of changes", "Aggregation".
- Don't put subtext in a header with a colon ("Append: insert-only history", "Merge: tables whose rows change"). The description belongs in the text below.
- Don't make headers into slogans or claims ("Trickles are tables, not a node type").
- Code identifiers in headers are fine on guide and reference pages (`` ## `pond.trickle(...)` ``).

Bullets:

- Avoid the "**Bold label**: description" or "**Bold label** — description" pattern as the default bullet form. It's fine occasionally (for example a short list of requirements), but when every bullet in the document looks like that, it reads as generated.
- Keep bullets parallel in form, and keep each one reasonably short. A bullet that grows into a paragraph should probably be a paragraph.

## Patterns to avoid

These patterns make text read as AI-generated, overwrought or padded. The first item is firm; the rest are soft, but lean strongly towards avoiding them.

- **Em-dashes (firm).** Don't use them anywhere, including code comments and table cells. Avoid en-dashes as sentence punctuation too. Use a full stop, comma, colon, parentheses, or restructure the sentence. En-dashes in number ranges are fine but a hyphen or "to" is simpler.
- **"Not X, but Y" / "X, not Y" framing.** For example "A Trickle isn't a node type, it's a table" or "A Tide is a staleness bound, not a schedule". Just say what the thing is. A contrast is fine when the reader genuinely holds the mistaken view and needs correcting, and then usually works better as an explanation ("A Tide looks like a schedule but works differently...").
- **"There is no X" statements** when nobody would reasonably assume X exists ("There is no `@trickle` decorator"). Describe how things work instead.
- **Stock phrases and filler**: "the thing that matters", "the key insight", "crucially", "importantly", "it's worth noting", "in short", "at its core", "this is where X comes in", "under the hood", "the beauty of", "simply put".
- **Showy or unusual vocabulary**: "load-bearing", "earns its keep", "honest boundary", "signature behaviour", "emergent", "does a lot of work", "for free" (as a flourish), "first-class", "batteries included" in running prose.
- **Aphoristic closers**: ending a section on a punchy summary line ("A schedule is a guess. Freshness is a fact."). End on the last useful piece of information.
- **Analogies that need their own explanation**, such as Kanban cards or CPU instruction pipelining. An analogy is only useful if the reader already knows the other domain well and it saves words. Usually a concrete example from the demo pipeline is better.
- **Rhetorical questions** as section openers or transitions.
- **Reflexive triplets** ("fast, simple and correct") where the three items weren't chosen for a reason.
- **Italics for emphasis** scattered through prose. Use sparingly, if at all.
- **Colons as a drumroll** ("The result: ..."). Colons introducing a list, definition or example are fine.

Again, don't over-apply these. A colon introducing an example is fine. The word "not" is fine. The problem is the pattern, not the individual character or word.

## Examples and accuracy

- Use the demo pipelines as running examples, since readers meet them in the Quickstart: `transactions`, `products` → `sales` → `reports`, and for incremental topics `orders`, `catalog` → `priced` → `revenue`. Concrete examples with real names and times ("if `transactions` last ran at 09:00 and `products` at 09:05...") are clearer than abstract ones.
- Check every number you use. The 3-second bottleneck and 7-second critical path in the freshness page were derived from the demo Ripple durations, not guessed.
- Check every behavioural claim against the code, not just against the existing docs. Several existing pages are out of date. When rewriting, fix stale claims rather than carrying them forward, and tell the author what you changed and why.
- If you can't confirm a claim, leave it out or flag it, rather than repeating it.
- Keep code examples runnable-looking and consistent (for example, aliasing sources the same way throughout a page).
- Don't break links. If you change a heading, check what links to its anchor.

## Terminology

- Use Duckstring's terms consistently and capitalise them as the docs do: Pond, Ripple, Trickle, Puddle, Catchment, Inlet, Outlet, Source, Sink, Pond Run, Tap, Wave, Pulse, Tide.
- Introduce each term once, clearly, at the point it's first needed. Don't front-load a glossary on concept pages; the intro page has one.
- On introductory pages, prefer plain descriptions over secondary Duckstring terms where they're equivalent: "the Ponds downstream of it" rather than "its Sinks", "everything upstream" rather than "its lineage". Keep the density of Duckstring vocabulary low enough that the page flows.
- When an established external term exists for a Duckstring concept (Z-set for weighted change rows), introduce the plain description first, then name the term, since it appears in the API.
- Don't invent new names in the docs for things the code doesn't name, unless the code's name is internal jargon that would confuse readers (for example "spine"). If you rename something for readability, say so to the author, since the docs and code should be kept findable from each other.

## Working with the author

- Ask questions in prose, not multiple-choice prompts.
- When producing a rewrite, briefly list what changed beyond wording: content moved, removed or corrected, claims you couldn't verify, and anything that depends on other pages (such as new anchors).
- The author's judgement on tone overrides this guide. If feedback on a page contradicts something written here, follow the feedback and update this guide.

## Firm rules

For quick reference, the rules that are not soft:

1. No em-dashes, anywhere.
2. Duckstring terms are introduced before they're relied on.
3. Behavioural claims and numbers are checked against the code.

Everything else is guidance. Use judgement, and choose clarity for the reader when in doubt.
