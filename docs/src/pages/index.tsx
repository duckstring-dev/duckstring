import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';

import styles from './index.module.css';

// The landing page (duckstring.com). Doubles as the canonical "what is this" intro — long enough
// that posting the bare link reads like the top of a launch post. It is styled on the product UI's
// dark canvas regardless of the site colour mode — the palette follows frontend/src/lib/store.ts.
//
// POSITIONING (settled — keep this order):
//   1. Brand statement + one-line "what is this".
//   2. Lead with the ENGINE'S BEHAVIOUR — the bidirectional throttle. The moat, and the wow.
//   3. Ground it in the package format (the mechanism that makes it need no config).
//   4. Close on the seamless upgrade (the daily-pain payoff), then Trickle as the reveal.
//   5. The lightweight on-ramp (wrap work you already run) + honest scope.
// Never call it an "orchestration framework"; never name competitors; don't lead with the Catchment.

// ─────────────────────────────────────────────────────────────────────────────
// A media placeholder. Drop a GIF/MP4/embed in where marked; the caption stays as the description
// of what the clip shows, so the page reads sensibly even before the media exists.
function DemoSlot({
  badge,
  frameLabel,
  children,
}: {
  badge: string;
  frameLabel: string;
  children: ReactNode;
}): ReactNode {
  return (
    <figure className={styles.demo}>
      <div className={styles.demoFrame}>
        {/*
          ▶ DROP MEDIA HERE ◀
          Replace the three placeholder spans below with ONE of:
            • a GIF/MP4:   <img src={useBaseUrl('/img/demo-xyz.gif')} alt="" className={styles.demoMedia} />
            • a video:     <video className={styles.demoMedia} autoPlay loop muted playsInline src={useBaseUrl('/img/demo-xyz.mp4')} />
            • an embed:    <iframe className={styles.demoMedia} src="https://www.youtube.com/embed/..." allowFullScreen />
          Keep the 16:9 .demoFrame wrapper so layout doesn't shift.
        */}
        <span className={styles.demoBadge}>{badge}</span>
        <span className={styles.demoPlay} aria-hidden>
          ▶
        </span>
        <span className={styles.demoLabel}>{frameLabel}</span>
      </div>
      <figcaption className={styles.demoCaption}>{children}</figcaption>
    </figure>
  );
}

function Section({
  kicker,
  title,
  children,
  alt,
}: {
  kicker?: string;
  title: string;
  children: ReactNode;
  alt?: boolean;
}): ReactNode {
  return (
    <section className={alt ? styles.sectionAlt : styles.section}>
      <div className={styles.sectionInner}>
        {kicker && <p className={styles.kicker}>{kicker}</p>}
        <h2 className={styles.sectionTitle}>{title}</h2>
        {children}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function Hero(): ReactNode {
  return (
    <header className={styles.hero}>
      <img src={useBaseUrl('/img/logo-mark.svg')} alt="" className={styles.mark} />
      <p className={styles.wordmark}>Duckstring</p>
      <h1 className={styles.tagline}>There is no DAG.</h1>
      <p className={styles.lead}>
        Build data pipelines the way you build software: version each transform, declare its
        dependencies, and Duckstring resolves the execution DAG automatically — then runs it on
        demand, with no schedules to write and no graph to govern.
      </p>
      <div className={styles.ctaRow}>
        <Link className={styles.ctaPrimary} to="/getting-started/quickstart">
          Quickstart
        </Link>
        <Link className={styles.ctaGhost} href="https://playground.duckstring.com">
          Try the playground
        </Link>
      </div>
      <code className={styles.pipInstall}>pip install duckstring</code>
    </header>
  );
}

// The 30-second "what is this", in prose. The unifying thesis: one decision, three payoffs.
function WhatIsThis(): ReactNode {
  return (
    <Section kicker="What is this?" title="One idea, three things you stop doing by hand.">
      <p className={styles.prose}>
        Duckstring is a runtime for data pipelines built on a single decision:{' '}
        <strong>treat each transform as a versioned package</strong> that declares its upstream
        dependencies, exactly the way a library declares the packages it imports. Make that one
        decision and three problems you normally solve by hand just dissolve:
      </p>
      <ul className={styles.payoffs}>
        <li>
          <strong>You stop building the DAG.</strong> The pipeline is the union of every package&apos;s
          declared dependencies — implicit in the graph, never written down or governed centrally.
        </li>
        <li>
          <strong>You stop writing schedules.</strong> Execution is driven by <em>demand</em> and{' '}
          <em>freshness</em>, not cron. You say how fresh an output must be; the runtime works out what
          to run and when, and throttles everything to its actual bottleneck.
        </li>
        <li>
          <strong>You stop recomputing unchanged data.</strong> Because each package boundary can keep
          history, downstream transforms can read only the rows that changed since they last ran.
        </li>
      </ul>
      <p className={styles.proseMuted}>
        It&apos;s built for <strong>new pipelines</strong> — greenfield ETL especially — where you get
        these properties for free instead of bolting them onto an existing scheduler. The packaging
        format is an open on-ramp; the demand-driven engine is the part worth showing first.
      </p>
    </Section>
  );
}

// THE HERO DEMO — the bidirectional throttle. The most unique behaviour, and the one no
// schedule-driven tool can reproduce.
function ThrottleDemo(): ReactNode {
  return (
    <Section
      kicker="The part nothing else does"
      title="A pipeline that re-paces itself — upstream and down."
      alt>
      <p className={styles.prose}>
        Everyone&apos;s scheduler can throttle work <em>downstream</em> of a slow step. Duckstring
        also throttles everything <em>upstream</em> of it. Demand flows back through the graph like
        Kanban cards, so a transform only runs when something downstream has actually asked for fresh
        output and there&apos;s new input to consume. Nothing over-produces results no one is waiting
        for.
      </p>

      <DemoSlot badge="Demo · hero clip" frameLabel="Live re-pacing when one Pond slows down">
        A streaming pipeline running at a steady cadence. We inject latency into a single Pond in the
        <em> middle</em> of the graph — and the whole pipeline, both upstream and downstream, re-paces
        to the new bottleneck in real time. No config is touched; nothing piles up or is dropped.
        Remove the latency and it speeds straight back up. <strong>Watch the upstream slow down too</strong>{' '}
        — that&apos;s the beat to call out; it&apos;s the part people don&apos;t expect.
      </DemoSlot>

      <p className={styles.proseMuted}>
        The receipts: a chain <code>A&nbsp;(1s) → B&nbsp;(3s) → C&nbsp;(1s)</code> under continuous
        demand. A naïve parallel scheme runs the one-second <code>A</code> three times for every run
        of <code>B</code> and throws two results away. Under Duckstring, <code>A</code> runs{' '}
        <em>once</em> per cycle — re-armed only when <code>B</code> actually consumes its output. The
        bottleneck sets the rhythm for the entire chain, with no rate limit configured anywhere. The
        full worked trace is in{' '}
        <Link to="/theory">the orchestration theory</Link>.
      </p>
    </Section>
  );
}

// The grounding: a hand-tinted pond.toml + the four triggers. (Manifest kept in sync with
// reference/pond-toml.md.)
function HowItWorks(): ReactNode {
  return (
    <Section kicker="Why it needs no configuration" title="The whole pipeline is in the manifests.">
      <div className={styles.manifestRow}>
        <pre className={styles.toml}>
          <span className={styles.tomlSection}>[pond]</span>{'\n'}
          name = <span className={styles.tomlString}>&quot;sales&quot;</span>{'\n'}
          version = <span className={styles.tomlString}>&quot;1.2.0&quot;</span>{'\n'}
          {'\n'}
          <span className={styles.tomlSection}>[sources]</span>{'\n'}
          transactions = <span className={styles.tomlString}>&quot;1.0.0&quot;</span>{'\n'}
          products = <span className={styles.tomlString}>&quot;1.1.0&quot;</span>
        </pre>
        <p className={styles.manifestCaption}>
          A transform is a <strong>Pond</strong>: a versioned Python package that declares its
          parents. Inside it, the individual operations are <strong>Ripples</strong> — ordinary
          Python functions, usually one per output table. Deploys are independent and atomic, like
          publishing a package; the pipeline is the union of every Pond&apos;s declared sources, so
          there is nothing to wire up and nothing global to maintain.
        </p>
      </div>

      <p className={styles.prose}>
        You never schedule a run. You attach <strong>demand</strong> to the output you care about, in
        one of four shapes — pull (keep me supplied) or push (bring me to this freshness), each as a
        one-shot or a standing request:
      </p>
      <div className={styles.triggers}>
        <div className={styles.triggerCell}>
          <span className={styles.triggerName}>Tap</span>
          <span className={styles.triggerKind}>pull · once</span>
          <span className={styles.triggerBody}>One resupply, propagated upstream.</span>
        </div>
        <div className={styles.triggerCell}>
          <span className={styles.triggerName}>Wave</span>
          <span className={styles.triggerKind}>pull · standing</span>
          <span className={styles.triggerBody}>Stay as fresh as the bottleneck allows.</span>
        </div>
        <div className={styles.triggerCell}>
          <span className={styles.triggerName}>Pulse</span>
          <span className={styles.triggerKind}>push · once</span>
          <span className={styles.triggerBody}>Run the lineage to <em>now</em>.</span>
        </div>
        <div className={styles.triggerCell}>
          <span className={styles.triggerName}>Tide</span>
          <span className={styles.triggerKind}>push · standing</span>
          <span className={styles.triggerBody}>Keep staleness under a bound (e.g. 1&nbsp;day).</span>
        </div>
      </div>
      <p className={styles.proseMuted}>
        A Tide is a staleness <em>bound</em>, not a cron line — &ldquo;never more than an hour
        old&rdquo;, and the runtime decides when to start work to honour it. A Wave isn&apos;t
        &ldquo;every N seconds&rdquo; at all; its frequency emerges from the pipeline&apos;s real
        bottleneck. Full semantics in <Link to="/guides/triggers">Triggers</Link>.
      </p>
    </Section>
  );
}

// THE CLOSER DEMO — seamless upgrade. The thing that's impossible today and hits the coordination
// pain squarely.
function UpgradeDemo(): ReactNode {
  return (
    <Section
      kicker="The daily-pain payoff"
      title="Ship a breaking change without a meeting."
      alt>
      <p className={styles.prose}>
        Ponds use SemVer, and a new major version runs <strong>concurrently</strong> with the old
        one. Deploy a breaking <code>v2</code> and it comes up <em>alongside</em> <code>v1</code>:
        existing consumers keep pulling <code>v1</code>, which keeps running, while consumers migrate
        to <code>v2</code> one at a time by changing a single line in their own manifest. The old
        major retires when nothing depends on it. No lockstep, no choreographed release, no freeze.
      </p>

      <DemoSlot badge="Demo" frameLabel="v1 and v2 of a Pond running side by side">
        A live Pond with downstream consumers. We deploy a <em>breaking</em> <code>v2</code> — and
        both majors run at once, <code>v1</code>&apos;s consumers undisturbed. Then we migrate one
        consumer by editing a single <code>pond.toml</code> line, and retire <code>v1</code> when the
        last consumer has moved. The line to land: <strong>nobody scheduled a meeting.</strong>
      </DemoSlot>

      <p className={styles.proseMuted}>
        This is the step a mesh of domain-owned pipelines usually stops short of: decentralised
        ownership without concurrent versions still forces every consumer to move together. See{' '}
        <Link to="/concepts/versioning">Versioning</Link>.
      </p>
    </Section>
  );
}

// THE REVEAL — incremental compute. Framed as a consequence of the package boundary, honest about
// the boundary of what's incremental.
function IncrementalReveal(): ReactNode {
  return (
    <Section kicker="And, because boundaries carry history…" title="Recompute only what changed.">
      <p className={styles.prose}>
        Because a Pond is a real package boundary, it can publish <strong>history</strong> instead of
        overwriting its tables. A downstream transform then reads only the rows that changed since it
        last ran — a small delta, not a full table — and the built-in incremental engine composes
        those deltas through joins and aggregations, so it recomputes just the output a change
        touches. It&apos;s incremental view maintenance (Z-sets / DBSP), wired straight into the
        package model.
      </p>

      <DemoSlot badge="Demo" frameLabel="A reprice touches one row, not the whole table">
        Side by side: an ordinary transform re-scanning its whole input every run, versus the same
        logic as an incremental one. A single upstream change (a new fact, a dimension reprice) flows
        through the join and aggregate touching only the affected output rows — the work scales with
        the <em>change</em>, not the size of the data.
      </DemoSlot>

      <p className={styles.proseMuted}>
        Stated honestly: this is the incremental-join and distributive-aggregation core done
        correctly at single-node scale. Holistic aggregates (median, exact distinct counts,
        percentiles) aren&apos;t maintained incrementally and fall back to a normal recompute — by
        design. The full surface is in <Link to="/guides/trickle">Incremental processing</Link>.
      </p>
    </Section>
  );
}

// THE ON-RAMP — the lightweight immediate win. A Ripple is generic code, so a Pond can wrap work you
// already run on other systems and simply stop re-running the parts that don't need it.
function OnRamp(): ReactNode {
  return (
    <Section
      kicker="Start small"
      title="Or wrap work you already run — and stop re-running it."
      alt>
      <p className={styles.prose}>
        A Ripple is just Python, so a Pond doesn&apos;t have to do the compute itself. It can wrap
        anything: a SQL transform, a local script, or a call out to a remote system that it kicks off
        and polls to completion. Point Duckstring at a sequence you already run and it becomes the
        coordinator — running each step only when its inputs have actually changed and something
        downstream wants the result. The immediate payoff is the redundant compute you stop paying
        for, with no rewrite.
      </p>
      <div className={styles.useCases}>
        <div className={styles.useCase}>
          <span className={styles.useCaseTitle}>Coordinate, don&apos;t migrate</span>
          <span className={styles.useCaseBody}>
            Wrap remote jobs behind a start-and-poll Ripple; Duckstring sequences them by demand and
            freshness, skipping the runs whose inputs are unchanged.
          </span>
        </div>
        <div className={styles.useCase}>
          <span className={styles.useCaseTitle}>Cut wasted runs</span>
          <span className={styles.useCaseBody}>
            The same change-gating that paces the demo means an expensive step doesn&apos;t fire when
            nothing upstream moved — the compute saving lands on day one.
          </span>
        </div>
        <div className={styles.useCase}>
          <span className={styles.useCaseTitle}>Grow into the model</span>
          <span className={styles.useCaseBody}>
            Promote a wrapped step to a native transform when it earns it; consumers don&apos;t
            change. The package boundary is the same either way.
          </span>
        </div>
      </div>
    </Section>
  );
}

// HONEST SCOPE — what it is and isn't for. Candour is a credibility asset with this audience.
function Scope(): ReactNode {
  return (
    <Section kicker="The honest boundary" title="What it&apos;s for — and what it isn&apos;t.">
      <div className={styles.scopeGrid}>
        <div className={styles.scopeGood}>
          <p className={styles.scopeHead}>Built for</p>
          <ul>
            <li>New pipelines, ETL especially, where you want the model from the start.</li>
            <li>Single-node transforms — comfortably into the tens of millions of rows.</li>
            <li>Teams that have felt the coordination wall of a large or mesh pipeline.</li>
            <li>Coordinating sequences of jobs to cut redundant compute, without a rewrite.</li>
          </ul>
        </div>
        <div className={styles.scopeBad}>
          <p className={styles.scopeHead}>Not (yet) for</p>
          <ul>
            <li>Distributed, petabyte-scale compute — this is a single-node runtime.</li>
            <li>Holistic incremental aggregates (median, percentile, exact distinct).</li>
            <li>A drop-in replacement for an existing scheduler you&apos;re happy with.</li>
          </ul>
        </div>
      </div>
    </Section>
  );
}

const ROUTES: {title: string; body: string; to?: string; href?: string}[] = [
  {
    title: 'Quickstart',
    body: 'A running four-Pond pipeline in a few minutes — install, deploy, trigger, query.',
    to: '/getting-started/quickstart',
  },
  {
    title: 'Playground',
    body: 'Feel freshness-based execution in your browser — build a graph, send demand, nothing to install.',
    href: 'https://playground.duckstring.com',
  },
  {
    title: 'Theory',
    body: 'The full demand-and-freshness model, worked step by step. The part to read if the demo intrigued you.',
    to: '/theory',
  },
  {
    title: 'GitHub',
    body: 'The source: the packaging standard, the CLI, and the reference runtime. Apache-2.0.',
    href: 'https://github.com/duckstring-dev/duckstring',
  },
];

function Routes(): ReactNode {
  return (
    <Section kicker="Go deeper" title="Where to next.">
      <div className={styles.routes}>
        {ROUTES.map((r) => (
          <Link key={r.title} className={styles.routeCard} to={r.to} href={r.href}>
            <span className={styles.routeTitle}>
              {r.title} <span className={styles.routeArrow}>→</span>
            </span>
            <span className={styles.routeBody}>{r.body}</span>
          </Link>
        ))}
      </div>
    </Section>
  );
}

function Hosting(): ReactNode {
  return (
    <section className={styles.hosting}>
      <p>
        Looking for cloud hosting — a Catchment run for you? I&apos;d like to hear from you:{' '}
        <a href="mailto:dev@duckstring.com">dev@duckstring.com</a>
      </p>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      description="Build data pipelines the way you build software: version each transform, declare its dependencies, and Duckstring resolves and runs the execution DAG on demand — no schedules, no governance.">
      <main className={styles.canvas}>
        <Hero />
        <WhatIsThis />
        <ThrottleDemo />
        <HowItWorks />
        <UpgradeDemo />
        <IncrementalReveal />
        <OnRamp />
        <Scope />
        <Routes />
        <Hosting />
      </main>
    </Layout>
  );
}
