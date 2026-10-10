# A guided tour for the Playground

Status: **built** (2026-10-10, branch `playground-tour`), except steps 12 to 14 (see "Deferred: the second
Tide"). The code is `playground/src/lib/{tour,tourSteps,tourStorage}.ts` and `components/TourCard.tsx`.

## Why

The Playground is Duckstring's most approachable asset: it runs in the browser, needs no install, and shows
pull orchestration better than any description. But a first-time visitor sees a graph of boxes and a sidebar
of controls with no idea what to press, and most will leave within a few seconds. A short guided tour turns
that first visit into a one-minute demonstration, and a link with the tour forced on (`?tour=1`) is something
worth posting.

## Requirements

- **Opt-in, and only offered once.** A first visit shows a small prompt ("Want the 5-minute tour?")
  with Start and Skip. Either choice is remembered, so repeat visitors go straight to the Playground.
- **Restartable.** A "Tour" button in the sidebar header (and the mobile bottom sheet's header) starts it
  again at any time.
- **Works on a phone.** Portrait phones (from 360 x 640) are a first-class target: every step readable and
  tappable with one thumb, nothing hidden behind the bottom sheet or the browser's own toolbars.
- **No backend.** The Playground makes no network calls today, and the tour mustn't change that.
- **Short chapters.** Three chapters of a few steps each, every step one or two short sentences, with a choice
  to continue or finish at the end of each chapter. Someone who stops after the first chapter has still seen the
  main idea.
- **No crowding on mobile.** The UI is already quite cramped, so the tour text needs to be shown and then hidden to display what it is introducing.

## How it works

### State and storage

- A small zustand slice (`tour: { step: number | null }`) next to the existing store, with `startTour`,
  `nextStep`, `prevStep` and `endTour(reason)`. Steps carry a `chapter`, and the last step of a chapter shows
  Continue and Finish instead of Next.
- The remembered choice lives in `localStorage` under `ds-playground-tour` (`done` or `skipped`). Every read
  and write is wrapped in try/catch, and a failure behaves like a first visit, so a private window or blocked
  storage still works.
- `?tour=1` starts the tour regardless of storage, and `?tour=0` suppresses the prompt (useful for embedding).

### Steps as data

Each step is a plain object, so the copy can be reviewed and edited without touching components:

```ts
interface TourStep {
  id: string;
  chapter: 1 | 2 | 3;
  title: string;
  body: string;                       // one or two sentences
  target?: { pond?: string; ripple?: string; control?: string };  // what to highlight
  setup?: (store: PlaygroundState) => void;   // runs when the step opens
  advance: 'next' | { until: (store: PlaygroundState) => boolean; fallbackMs: number };
  action?: { label: string; run: (store: PlaygroundState) => void };  // "Do it for me"
}
```

- `setup` drives the store directly (reset the graph, set speed, place a trigger), so the tour never depends on
  the visitor tapping a small target precisely.
- `advance: { until }` waits for something in the simulation (for example the run chart's intervals settling),
  with a fallback timer so the tour can't hang. While a step waits, the tour raises the simulation speed so
  start-up and steady state take seconds, not minutes, and restores the visitor's speed when it ends.
- Steps that invite the visitor to press a real control also offer a button that does it for them. That keeps
  the hands-on feel on desktop without making a phone visitor hunt for a button.

### Store changes

- Add `resetToDemo()`, which rebuilds `buildDemoState()` and clears triggers, logs and selection. The tour calls
  it on start. If the visitor has changed the graph, starting the tour asks first.
- Targets are named by Pond and Ripple names in the demo graph (`reports`, `sales.join_lines`), resolved to ids
  at run time, so ids changing between builds doesn't matter.
- The Tide input needs to accept a value from the tour (step 10 prefills 5 s), and the tour needs a way to add
  a Pond with a given name and Source (steps 12 and 13), which `addPond` plus `linkPonds` already cover.

### Highlighting

- Canvas targets: `PondNode`/`RippleNode` read a `tourTarget` id from the store and draw a pulsing ring in
  `THEME_BRAND` cyan. The canvas frames the target with the existing `fitView({ nodes: [...] })` call that the
  mobile tap-to-zoom already uses.
- Sidebar targets: the Wave button, the Tide input, a Ripple's duration and variability (σ), and the run chart
  (`TraceChart`) get `data-tour` attributes, and the tour adds an outline class to the matching element. The
  tour selects the Pond or Ripple first (`selectPond`/`selectRipple`), so the right panel is showing.
- No full-screen dimmed overlay. On a phone it blocks the canvas, swallows taps and fights the bottom sheet.
  The ring and outline are enough.
- Animations respect `prefers-reduced-motion` (no pulse, `fitView` without duration).

### The tour card

- **Desktop and tablet:** a card floating over the bottom-left of the canvas (about 320 px wide), clear of the
  sidebar and console.
- **Phone portrait:** discuss this with the user, but likely a modal popup with 'next' that hides the text and shows just the UI for a set time after before bringing up the next popup. When highlighting UI elements, possibly the card is positioned in the top or bottom third to allow visibility for the target element.
- Buttons are Back, Next (Continue and Finish at the end of a chapter), and a small Skip. Tap targets at least
  44 px high. A progress indicator per chapter ("Chapter 1 · 3 of 6").
- Keyboard: Escape skips, arrow keys move. The card body is `aria-live="polite"` so screen readers announce each
  step, and focus moves to the card when it opens.

## Steps

The author's outline (2026-10-09), with wording edits. It uses the demo graph: `transactions`, `products` →
`sales` → `reports`, where `sales.join_lines` takes 3 s and the critical path is about 8 s. The copy follows
the docs' terminology rules (Tide leads with "every x" and gets to "at least as fresh as"; Pulse and Tide
aren't called push triggers). Curly brackets say what the tour does on screen.

### Chapter 1: running at the bottleneck's pace

1. Here's an example pipeline. It's split into four modules (Ponds), each containing steps (Ripples).
   {Fit the whole graph.}
2. Each Ripple shows how long it takes to run. This one is the longest, at 3 s, so the most often this
   pipeline could run is every 3 s. {Select `sales.join_lines`.}
3. Ideally, shorter Ripples like this one wouldn't run more often than that. To do this, Duckstring runs from
   the *end* of the pipeline instead of the beginning, sending demand to everything upstream.
   {Select a 1 s Ripple, then `reports`.}
4. A Wave trigger sends demand every time its Pond finishes, so the pipeline runs as often as it can.
   {Highlight the Wave button on `reports`; "Next" presses it and hides the tour for enough time that the pipeline reaches steady state}
5. At steady state, every Ripple - both upstream and downstream - is running every 3 s. Note that this means the `sales` Pond
   is actually running twice simultaneously
   {Fit the whole graph}
6. You can check each Ripple's run time and time between runs with its run chart. Every one shows 3 s between runs. 
   {Select another Ripple; highlight its run chart.}

### Chapter 2: noise

7. Let's make the bottleneck's run time vary randomly. {Select `join_lines`; highlight σ and set it to 0.2.}
8. The pipeline adjusts to the varying bottleneck. Nothing is tuned and nothing plans globally: each step only
   reacts to the steps next to it. {Highlight the same run chart as step 6.}

### Chapter 3: fixed frequencies and several outputs

9. Sometimes you don't need data as fresh as possible, just at a set frequency (e.g. daily). That's what a Tide is for.
   Let's set one for every 5 s. {Highlight the Tide control on `reports`, prefilled with 5 s.}
10. This requests a new run from the beginning of the pipeline any time the data on the Pond is older than 5 s.
    {Fit the whole graph; highlight the run chart.}
11. Let's add a new output Pond reading from `sales`. It doesn't run yet, because there's no trigger on it. That's a key
    advantage to scheduling from the end - nothing runs if it's not required.
    {Add the Pond, linked as a Sink of `sales`.}
12. Now let's put another Tide on it. {Add a 5s Tide}
13. Both terminal Ponds receive their updated data simultaneously, so they naturally synchronise their requests 
    {Select an upstream Pond; highlight its run chart.}
14. Meanwhile each output still gets the freshness it asked for: `reports` keeps updating every 5 s.
    {Select `reports`; highlight its run chart.}
15. That's most of it! The docs cover the rest, from Windows and failures to versioning and incremental
    processing. {Links to the docs and Quickstart, and a Close button.}


## Testing

- The Playground has no test setup today. Add Playwright with three viewports (360 x 640 and 390 x 844 portrait
  phones, 1280 x 800 desktop) and one script that runs the tour end to end with "Next" on every step,
  asserting each step's advance condition fires and saving a screenshot per step.
- Unit test the storage wrapper (blocked storage behaves like a first visit) and `?tour=` handling.
- Manual check on a real iPhone (Safari) and Android phone (Chrome) through a Vercel preview deploy, which also
  makes the review possible from a phone.

## Measuring it

`@vercel/analytics` is already installed. If the Vercel plan supports custom events, record `tour_start`,
`tour_complete` and `tour_skip` with the step reached, to see where people drop off.

## As built

- Phone portrait: the card spans the screen width, at the top when the step outlines a sidebar control (so
  the open bottom sheet stays visible) and at the bottom otherwise. The canvas framing pads whichever edge
  the card covers. A pause after every step (card hidden so the visitor sees what it covered) was tried and
  dropped: only the Wave's start-up needs it, and that's already a wait.
- Phone landscape (and any screen under 500 px tall above the phone breakpoint) gets the desktop layout, so
  the card is a flat bar flush along the bottom of the canvas: one line for the chapter and Skip, then text
  on the left (scrolling if it must) and buttons on the right. The canvas's zoom controls hide while it's up.
  Tested at 844 x 390. Phones narrower than 768 px in landscape (667 x 375) still get the portrait layout,
  because the app's breakpoint is width-only.
- While a step waits on the simulation, the card shrinks to a bar with "Skip ahead" and a progress bar,
  paced by each wait's measured typical time (`expectedMs`: 17.5 s, 2.9 s, 3.7 s): linear to 90%, then
  creeping, so a slow wait never looks frozen or finished.
  During a wait nothing is selected or highlighted and the mobile bottom sheet closes, so the running
  pipeline has the whole screen; the next step selects what it needs. The Wave's start-up (the
  visitor's first look at the pipeline running) plays at 1x and settles in about 20 s; the later waits run
  at 10x. Speed returns to 1x when a wait ends, and the visitor's own speed comes back when the tour ends.
- Highlights: on each step a white pill fades in as it contracts onto the target from about 160 px out,
  over about a second, while the rest of the screen dims slightly (`TourSpotlight`, which tracks the target every frame as the canvas pans), then fades
  to a steady pulsing white ring on the node or control. White because every other colour on the canvas
  means a state; a cyan ring read as one more running node. When a step has both a node and a control, both
  contract at once. The dim never takes pointer events and doesn't persist.
- The closing step suggests what to try next (a trigger on `forecast`; more Ponds to see how more complex
  dependencies interact) before the docs links. On a phone the card's header and buttons stay put and only
  its text scrolls, so Close is always reachable at 360 x 640.
- Step 3 is two steps (`transactions.ingest`, then `reports`), one sentence each, so the highlight doesn't
  jump while the card is up. Chapter 1 has seven steps.
- Pressing the real control (Wave, Tide, σ) also advances the tour, as the card's button would.
- The mobile bottom sheet's open state, the Tide input's draft value and a `formEpoch` (remounts the
  uncontrolled sidebar inputs after the tour sets a value) moved into the Playground store.
- Chapter 3 turns the σ on `join_lines` back to 0 first: with σ = 0.2 the Tide's cadence jitters around 5 s,
  which undercuts "every 5 s".
- Copy changes from the outline: step 5 says `sales` takes 5 s from start to finish (so two runs overlap)
  instead of "running twice simultaneously"; step 10 describes the Tide as asking for newer data every 5 s
  rather than "any time the data is older than 5 s", because with an 8 s lead the data at `reports` is
  always 5 to 10 s old.
- Analytics: `tour_start`, `tour_complete` and `tour_skip` (with the step reached; 0 means the first-visit
  offer was declined) through `@vercel/analytics` `track`. They're dropped if the plan has no custom events.

## Deferred: the second Tide (steps 12 to 14)

Simulated against `orchestration.ts`, two 5 s Tides only line up when they fire on the same tick. Set at any
other moment (as the tour would, or a visitor pressing Set), the second Tide runs out of phase: `sales` and
the Inlets run every 3 s instead of 5 s, and `reports` and the new Pond alternate 3 s and 6 s indefinitely.
A Wake on the new Pond first doesn't help, because with an 8 s lead and a 5 s period `reports` always holds
a pending target, so the Tide clock is the wall-clock time of its last push. So steps 13 ("they naturally
synchronise") and 14 ("`reports` keeps updating every 5 s") aren't true of the engine today. This is the
"two Tides at different times stack" concern from the 2026-10-10 Tide study; aligned Tides (request the
latest multiple of the period from a fixed anchor) would make the steps true.

The tour stops after step 11 (the untriggered Pond doesn't run) and goes to the closing step. To restore the
steps once Tides align, add them back to `TOUR_STEPS` after `new-output`: a `second-tide` step (action: a 5 s
Tide on `forecast`, wait until `forecast`'s completions settle), then `synchronised` (`sales` run chart) and
`freshness-kept` (`reports` run chart). The end-to-end test asserts each wait ends on its condition, so it
would catch a regression.

## Out of scope

- Tours of the Catchment's web UI (`frontend/`), which is a separate app.
- Translations.
- Saving or sharing a user's own graph.
