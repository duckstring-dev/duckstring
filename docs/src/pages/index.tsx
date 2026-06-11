import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';

import styles from './index.module.css';

// The landing page (duckstring.com). Deliberately sparse: the brand statement, just enough of the
// model to intrigue, and routes out to the docs, the playground, and GitHub. It is styled on the
// product UI's dark canvas regardless of the site colour mode — copy and palette follow
// brand/copy.md and frontend/src/lib/store.ts.

function Hero() {
  return (
    <header className={styles.hero}>
      {/* <div className={styles.rippleStack}>
        <span className={styles.ripple} />
        <span className={styles.ripple} />
        <span className={styles.ripple} />
        
      </div> */}
      <img src={useBaseUrl('/img/logo-mark.svg')} alt="" className={styles.mark} />
      <p className={styles.wordmark}>Duckstring</p>
      <h1 className={styles.tagline}>There is no DAG.</h1>
      <p className={styles.lead}>
        Build data pipelines the way you build software: version each transform, declare its
        dependencies, and Duckstring resolves the execution DAG automatically.
      </p>
      <div className={styles.ctaRow}>
        <Link className={styles.ctaPrimary} to="/intro">
          Read the docs
        </Link>
        <Link className={styles.ctaGhost} href="https://playground.duckstring.com">
          Try the playground
        </Link>
      </div>
      <code className={styles.pipInstall}>pip install duckstring</code>
    </header>
  );
}

// A hand-tinted pond.toml — the whole pitch in one file. (Kept in sync with reference/pond-toml.md.)
function Manifest() {
  return (
    <section className={styles.manifest}>
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
        parents. Deploys are independent, majors run side by side, and the pipeline is implicit in
        the package graph — nobody builds it, draws it, or governs it.
      </p>
    </section>
  );
}

const ROUTES: {title: string; body: string; to?: string; href?: string}[] = [
  {
    title: 'Docs',
    body: 'The model, a quickstart to a running four-Pond pipeline, and the full orchestration theory.',
    to: '/intro',
  },
  {
    title: 'Playground',
    body: 'Feel freshness-based execution in your browser — build a graph, send demand, nothing to install.',
    href: 'https://playground.duckstring.com',
  },
  {
    title: 'GitHub',
    body: 'The source: the packaging standard, the CLI, and the reference Catchment runtime.',
    href: 'https://github.com/duckstring-dev/duckstring',
  },
];

function Routes() {
  return (
    <section className={styles.routes}>
      {ROUTES.map((r) => (
        <Link key={r.title} className={styles.routeCard} to={r.to} href={r.href}>
          <span className={styles.routeTitle}>
            {r.title} <span className={styles.routeArrow}>→</span>
          </span>
          <span className={styles.routeBody}>{r.body}</span>
        </Link>
      ))}
    </section>
  );
}

function Hosting() {
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
      description="Build data pipelines the way you build software: version each transform, declare its dependencies, and Duckstring resolves the execution DAG automatically.">
      <main className={styles.canvas}>
        <Hero />
        <Manifest />
        <Routes />
        <Hosting />
      </main>
    </Layout>
  );
}
