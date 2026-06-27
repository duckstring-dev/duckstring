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
//   1. Brand statement + one-line "what is this". pip install is the primary conversion — weight it.
//   2. Lead with the ENGINE'S BEHAVIOUR — the bidirectional throttle. The moat, and the wow.
//   3. Ground it in the package format (the mechanism that makes it need no config).
//   4. Close on the seamless upgrade (the daily-pain payoff), then Trickle as the reveal.
//   5. The lightweight on-ramp (wrap work you already run) + honest scope.
// Never call it an "orchestration framework"; never name competitors; don't lead with the Catchment.
// FRAMING: lead with the positive value (implicit architecture, demand-driven, incremental) rather
// than the "things you stop doing" negation — the payoff, not the absence.

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
      <div className={styles.install}>
        <span className={styles.installPrompt} aria-hidden>
          $
        </span>
        <code className={styles.installCmd}>pip install duckstring</code>
      </div>
      <p className={styles.lead}>
        Build data pipelines the way you build software: version each transform, declare its
        dependencies, and Duckstring forms and runs only the DAG paths that are demanded.
      </p>
      <div className={styles.ctaRow}>
        <Link className={styles.ctaPrimary} to="/getting-started/quickstart">
          Quickstart →
        </Link>
        <Link className={styles.ctaGhost} href="https://playground.duckstring.com">
          Try the playground
        </Link>
      </div>
      <p className={styles.heroNote}>Apache-2.0 · pure Python · no service to stand up</p>
    </header>
  );
}

// The 30-second "what is this", in prose. The unifying thesis: one decision, three payoffs — framed
// as what you gain, not what you give up.
function WhatIsThis(): ReactNode {
  return (
    <Section kicker="Effortless Governance" title="You should only care about who you consume from">
      <p className={styles.prose}>
        Duckstring operates on a core decision:{' '}
        <strong>treat each transform as a versioned package</strong> (a Pond) that declares its upstream
        dependencies, exactly the way a library declares the packages it imports. Make that one
        decision and you get three things that are normally hand-built and hand-tended for free:
      </p>
      <ul className={styles.payoffs}>
        <li>
          <strong>DAG is implied.</strong> The pipeline is the union of every Pond&apos;s
          declared dependencies. There&apos;s no central DAG to build, wire, or govern — it&apos;s
          already in the graph.
        </li>
        <li>
          <strong>Demand-driven execution.</strong> Run from the <outputs>, not inputs, and paths with no downstream consumers sit idle. Each path runs only as often as its bottleneck - both downstream <and upstream>. Adding new Ponds (or adding breaking changes to an old one) will not execute until there are consumers ready to use it.
        </li>
        <li>
          <strong>Native incremental processing.</strong> Run history is metadata, making change detection and incremental processing trivial. Duckstring bundles Trickle: a DBSP-based incremental engine running on DuckDB. Blazing fast execution on a single node - perfect for the 90% of cases where you don't *actually need* distributed compute.
        </li>
      </ul>
      <p className={styles.proseMuted}>
        The framework is generic - attach any python code (even calls to external services) and get all the orchestration benefits immediately.
      </p>
    </Section>
  );
}

// THE HERO DEMO — the bidirectional throttle. The most unique behaviour, and the one no
// schedule-driven tool can reproduce.
function ThrottleDemo(): ReactNode {
  return (
    <Section
      kicker="Benefits of demand-driven control"
      title="Bottleneck-aware execution. No wasted compute."
      alt>
      <p className={styles.prose}>
        Most schedulers can throttle work <em>downstream</em> of a slow step. Duckstring throttles
        everything <em>upstream</em> of it too. Execution is strictly demand-driven: a transform runs
        only when something downstream has actually asked for it. The result is a pipeline that re-paces itself to its real bottleneck — and never
        over-produces results no one is waiting for.
      </p>

      <DemoSlot badge="Demo · hero clip" frameLabel="Live re-pacing when one Pond slows down">
      </DemoSlot>
      <p className={styles.proseMuted}>
        No sophisticated prediction of run times is required - simply flipping to control by consumers and not suppliers means the entire path naturally throttles to the slowest process.
See{' '}
        <Link to="/orchestration_theory">Orchestration Theory</Link>.
    </Section>
  );
}

// THE CLOSER DEMO — seamless upgrade. The thing that's impossible today and hits the coordination
// pain squarely.
function UpgradeDemo(): ReactNode {
  return (
    <Section
      kicker="Upgrade atomically"
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
        Upgrading a complex sequence of transformations can paralyze development, especially if upstream changes are needed. Just deploy breaks as a separate Pond, know that it won't run until downstream also upgrades, and be sure you won't have broken anything. See{' '}
        <Link to="/concepts/versioning">Versioning</Link>.
      </p>
    </Section>
  );
}

// THE REVEAL — incremental compute. Framed as a consequence of the package boundary, honest about
// the boundary of what's incremental.
function IncrementalReveal(): ReactNode {
  return (
    <Section kicker="Keep tasks small with deltas" title="Discrete stages for seamless incremental processing">
      <p className={styles.prose}>
        Incremental processing becomes very natural once a Pond has a clear lineage, runs only when parents change, and tracks an epoch throughout. Bundled with Duckstring is the Trickle engine - a DBSP implementation over DuckDB that cuts processing to the absolute minimum by focussing only on changes.
      </p>

      <DemoSlot badge="Demo" frameLabel="A reprice touches one row, not the whole table">
        Side by side: an ordinary transform re-scanning its whole input every run, versus the same
        logic as an incremental one. A single upstream change (a new fact, a dimension reprice) flows
        through the join and aggregate touching only the affected output rows — the work scales with
        the <em>change</em>, not the size of the data.
      </DemoSlot>

      <p className={styles.proseMuted}>
        Done well, incremental processing allows you to stay single-node, in-memory and blazing fast - real streaming performance with minimal infrastructure.
See {' '}
        <Link to="/guides/trickle">Incremental processing</Link>.
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
      title="Drop it into the stack you already run."
      alt>
      <p className={styles.prose}>
        You don&apos;t have to move your compute to get value. A Pond is just python, so can
        wrap anything: a SQL transform, a local script, or a call out to a remote system that it kicks
        off and polls to completion. Point Duckstring at a sequence you already run and it becomes the
        coordinator — firing each step only when its inputs have actually changed and something
        downstream wants the result. The redundant compute you stop paying for lands on day one, with
        no rewrite.
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
