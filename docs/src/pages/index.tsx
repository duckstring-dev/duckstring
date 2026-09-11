import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';

import styles from './index.module.css';

// ─────────────────────────────────────────────────────────────────────────────
interface DemoSlotProps {
  src: string;        // Path to video (e.g., '/img/demo-analytics') without extension
  aspectRatio?: string; // Optional: Allows custom aspect ratios like '16/9', '4/3', '1/1'
  poster?: string;     // Optional: Path to a static fallback screenshot
}

function DemoSlot({ 
  src, 
  aspectRatio = '16/9', 
  poster 
}: DemoSlotProps): ReactNode {
  return (
    <figure className={styles.demo}>
      <div 
        className={styles.demoFrame} 
        style={{ aspectRatio }} // Inline style handles dynamic aspect ratios natively
      >
        <video 
          className={styles.demoMedia} 
          autoPlay 
          loop 
          muted 
          playsInline 
          poster={poster}
        >
          {/* Delivers WebM first for optimized browsers, falls back to MP4 */}
          <source src={useBaseUrl(`${src}.webm`)} type="video/webm" />
          <source src={useBaseUrl(`${src}.mp4`)} type="video/mp4" />
          Your browser does not support the video tag.
        </video>

        <span className={styles.demoPlay} aria-hidden>
          ▶
        </span>
      </div>
    </figure>
  );
}

function Section({
  id,
  title,
  children,
  alt,
}: {
  id?: string;
  title: string;
  children: ReactNode;
  alt?: boolean;
}): ReactNode {
  return (
    <section id={id} className={alt ? styles.sectionAlt : styles.section}>
      <div className={styles.sectionInner}>
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
      <div className={styles.brand}>
        <img src={useBaseUrl('/img/logo-mark.svg')} alt="" className={styles.mark} />
        <div className={styles.brandText}>
          <h1 className={styles.wordmark}>Duckstring</h1>
          <p className={styles.lead}>Get your ducks in a row.</p>
        </div>
      </div>
      <div className={styles.installRow}>
        <div className={styles.install}>
          <span className={styles.installPrompt} aria-hidden>
            $
          </span>
          <code className={styles.installCmd}>pip install duckstring</code>
        </div>
        <Link className={styles.ctaPrimary} to="/getting-started/quickstart">
          Quickstart →
        </Link>
      </div>
    </header>
  );
}

// Intro
function WhatIsThis(): ReactNode {
  return (
    <Section title="The Data Engineering platform built on DuckDB.">
      <p className={styles.prose}>
        The world has been upsold on distributed computing. 
        Data volumes into the terabytes can easily be handled on single machines, where DuckDB has no competition.
        Duckstring is an open source data engineering platform that gives DuckDB the space to do its magic - <b>fast, simple scalable</b>.
      </p>
      <p>
        Duckstring runs the same on your local machine, a single Cloud box, or coordinating across many compute instances
        for more complex pipelines, allowing you to start small and scale up whenever you need.
      </p>
      <p>
        It is <b>opinionated</b>, but <b>flexible</b>:
      </p>
      <ul className={styles.payoffs}>
        <li>
          <a className={styles.payoffLink} href="#upgrade">
            <strong>Modular Transformations.</strong>
            <span className={styles.payoffArrow}> ↓</span>
          </a>{' '}
          Every collection of transformations and business logic is encapsulated into version-controlled Ponds. 
          Each declares their versioned dependencies - the DAG is implied, allowing you to treat transformations like software packages.
        </li>
        <li>
          <a className={styles.payoffLink} href="#demand">
            <strong>Pull Orchestration.</strong>
            <span className={styles.payoffArrow}> ↓</span>
          </a>{' '}
          Orchestration is set from the perspective of the pipeline <b>end</b>. 
          Nothing will execute unless something is actually consuming it.
        </li>
        <li>
          <a className={styles.payoffLink} href="#incremental">
            <strong>Modern Incrementality.</strong>
            <span className={styles.payoffArrow}> ↓</span>
          </a>{' '}
          Changes are detected and propagated across Ponds using DBSP - a data format allowing tree-joins to skip computing 
          parts of a transformation that could not have changed.
        </li>
        <li>
          <a className={styles.payoffLink} href="#catalog">
            <strong>Data Lives With Code.</strong>
            <span className={styles.payoffArrow}> ↓</span>
          </a>{' '}
          The orchestrator and catalog live in Duckstring's Catchment - an environment
          for coordinating Ponds and their interactions.
        </li>
      </ul>
      <p className={styles.proseMuted}>
        <a className={styles.payoffLink} href="#start">
          <strong>Python Based and CLI-first</strong>
          <span className={styles.payoffArrow}> ↓</span>
        </a>{' '}
        Ponds are generic python, with all the flexibility that provides.
      </p>
    </Section>
  );
}

// Modular Transformations
function ModularTransformations(): ReactNode {
  return (
    <Section id="upgrade" title="Treat transformations like software packages." alt>
      <p className={styles.prose}>
        Ponds use strict SemVer conventions. A new major version runs <strong>concurrently</strong> with the old
        one, and doesn't start until something is consuming from it. 
        The headache of choreographic complex changes is avoided using the same techniques that allowed open source to flourish.
        Know your family, not the world.
      </p>

      <DemoSlot src="/img/upgrade" aspectRatio="28/10"/>

      <p className={styles.proseMuted}>
        Upgrading a complex sequence of transformations can paralyze development. 
        Just deploy breaks as a separate major-version Pond, and let them sit 
        unused until consumers upgrade. See{' '}
        <Link to="/concepts/versioning">Versioning</Link>.
      </p>
    </Section>
  );
}

// Pull Orchestration
function PullOrchestration(): ReactNode {
  return (
    <Section
      id="demand"
      title="Only run what's used."
      alt>
      <p className={styles.prose}>
        Push Orchestration sets schedules relative to the <b>start</b> of a pipeline, and 
        throttles work <em>downstream</em> of a slow step, and blindly runs pipelines that have no consumers.
        Setting schedules instead at the <b>end</b> of a pipeline throttles both upstream and downstream of a slow step, and ensures
        that only paths with consumers are kept fresh.
      </p>

      <DemoSlot src="/img/ripple" aspectRatio="28/10"/>

      <p className={styles.proseMuted}>
        No sophisticated prediction of run times is required. Duckstring uses the same scheduling system that keeps
        modern manufacturing processes humming. See{' '}
        <Link to="/theory">Orchestration Theory</Link>.
      </p>
    </Section>
  );
}

// Modern Incrementality
function ModernIncrementality(): ReactNode {
  return (
    <Section
      id="incremental"
      title="Only compute what's changed.">
      <p className={styles.prose}>
        Incremental approaches are typically hand-rolled and need careful engineering. Duckstring's 
        DBSP-based <em>Trickle</em> package includes all standard <em>distributive</em> and <em>algebraic</em> methods 
        in its query builder, so you don't have to think about it. 
        Using DBSP methods and changed-key detection, built queries are optimized to 
        skip any computation that could not have changed since the previous run. Done well, even massive data transformations
        can approach streaming-level latencies.
      </p>

      <DemoSlot src="/img/trickle" aspectRatio="28/10"/>

      <p className={styles.proseMuted}>
        Duckstring-managed change processing, so you see the performance without the pain. See{' '}
        <Link to="/guides/trickle">Incremental processing</Link>.
      </p>
    </Section>
  );
}

// Data Lives With Code
function Catalog(): ReactNode {
  // TODO: Include catalog recording
  return (
    <Section id="upgrade" title="Logic sets location." alt>
      <p className={styles.prose}>
        The execution environment is the Catchment, which also governs the catalog.
        Schemas are strictly defined as being within each major Pond version,
        and objects and tables are enumerated within them. If you have a Catchment,
        you have a catalog, governance and lineage.
      </p>

      {/* <DemoSlot src="/img/catalog" aspectRatio="28/10"/> */}

      <p className={styles.proseMuted}>
        Keeping track of where data is need not be a separate task to defining how it's generated. See{' '}
        <Link to="/concepts/catalog">Catalog</Link>.
      </p>
    </Section>
  );
}

// Get started
function GetStarted(): ReactNode {
  return (
    <Section id="start" title="Everything is a Pond." alt>
      <p className={styles.prose}>
        There's nothing stopping you taking your existing transformations and calling them a Pond.
        Import straight SQL, a dbt model or Ibis transformations. You can even simply call an external
        service, using Duckstring as the scheduler alone.
      </p>
    </Section>
  );
}

const ROUTES: {title: string; body: string; to?: string; href?: string}[] = [
  {
    title: 'Quickstart',
    body: 'Get a pipeline running on your local machine in minutes.',
    to: '/getting-started/quickstart',
  },
  {
    title: 'Orchestration Playground',
    body: 'Experiment with Pull-based scheduling with a browser toy.',
    href: 'https://playground.duckstring.com',
  },
  {
    title: 'Documentation',
    body: 'Full package documentation.',
    to: '/intro',
  },
  {
    title: 'GitHub',
    body: 'See the source code.',
    href: 'https://github.com/duckstring-dev/duckstring',
  },
];

function Routes(): ReactNode {
  return (
    <Section title="">
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
        Want the platform cloud-hosted for you? Contact:{' '}
        <a href="mailto:dev@duckstring.com">dev@duckstring.com</a>
      </p>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      description="The Data Engineering platform built on DuckDB. Fast, simple, scalable.">
      <main className={styles.canvas}>
        <Hero />
        <WhatIsThis />
        <ModularTransformations />
        <PullOrchestration />
        <ModernIncrementality />
        <Catalog />
        <GetStarted />
        <Routes />
        <Hosting />
      </main>
    </Layout>
  );
}
