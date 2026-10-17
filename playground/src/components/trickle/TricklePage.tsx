'use client';

import Link from 'next/link';
import { THEME_BRAND } from '@/lib/store';
import { JoinGrid } from './JoinGrid';
import { KeySet } from './KeySet';
import { Pipeline } from './Pipeline';
import { ZSetFigure } from './ZSetFigure';
import { FAINT, MONO, MUTED, SANS, TEXT } from './theme';

// /trickle: how a Trickle keeps a table up to date from its changes. Four sections, each with a figure: changes
// as weighted rows, a join's change as a grid, narrowing by key, and the demo pipeline run by run.

const DOCS = 'https://docs.duckstring.com/concepts/trickles';

export function TricklePage() {
  return (
    <main style={{ fontFamily: SANS, color: TEXT, width: '100%', overflowX: 'hidden' }}>
      <div style={{ maxWidth: 820, margin: '0 auto', padding: '20px 16px 64px' }}>
        <nav style={{ display: 'flex', alignItems: 'center', gap: 12, fontFamily: MONO, fontSize: 12, marginBottom: 28 }}>
          <Link href="/" style={{ color: MUTED, textDecoration: 'none' }}>← Playground</Link>
          <span style={{ color: FAINT }}>/</span>
          <span style={{ color: TEXT }}>Trickle</span>
          <a href={DOCS} target="_blank" rel="noopener noreferrer" style={{ marginLeft: 'auto', color: THEME_BRAND, textDecoration: 'none' }}>
            Docs ↗
          </a>
        </nav>

        <header style={{ marginBottom: 36 }}>
          <h1 style={{ fontSize: 34, lineHeight: 1.15, margin: '0 0 12px', fontWeight: 700 }}>Trickle</h1>
          <p style={lead}>
            A step in a Duckstring pipeline normally rewrites its tables in full on every run, even when only a few rows
            changed. A <b>Trickle</b> is a table that keeps a record of its changes, so the tables built from it can be
            updated by working only on what changed. This page shows how, step by step, then runs a small pipeline
            modelled on the Trickle demo in Duckstring.
          </p>
        </header>

        <Section n={1} title="Changes are rows with a weight">
          <p style={para}>
            Each change is stored as the whole row, with a weight: <Code>+1</Code> for a row added and <Code>-1</Code> for a
            row removed. An update is both, the old row removed and the new one added. A set of rows with weights is
            called a <b>Z-set</b>.
          </p>
          <ZSetFigure />
          <p style={para}>
            So combining changes is addition and joining is multiplication, for inserts, updates and deletes alike. That
            is what lets a join be updated from its changes.
          </p>
        </Section>

        <Section n={2} title="A join, one run later">
          <p style={para}>
            After a run, each table is its previous state plus its changes: <Code>A′ = A + ΔA</Code>. Here{' '}
            <Code>A</Code> is the order lines and <Code>B</Code> the catalog. Their join expands like a product, and the
            part made only of previous states, <Code>A ⋈ B</Code>, is last run&rsquo;s output, which is already stored.
          </p>
          <JoinGrid />
          <p style={para}>
            Every term uses the tables as they are after the run, so no earlier version of a table ever has to be
            rebuilt.
          </p>
        </Section>

        <Section n={3} title="Only matching keys take part">
          <p style={para}>
            The two strips still span a whole table each. But a join only pairs rows with equal keys, so in{' '}
            <Code>A′ ⋈ ΔB</Code> only the order lines for the repriced products can take part, and in <Code>ΔA ⋈ B′</Code>{' '}
            only the products that the new order lines name.
          </p>
          <KeySet />
          <p style={para}>
            Semi, anti and outer joins also depend on whether a key has any match at all. Only a key in the changes can
            gain its first match or lose its last, so the same narrowing applies. When one side of a join is itself a
            join, the changed keys are passed down to the table that holds them.
          </p>
        </Section>

        <Section n={4} title="A run, end to end">
          <p style={para}>
            The pipeline has four Ponds, Duckstring&rsquo;s packages of transformations. <Code>orders</Code> appends order lines and <Code>catalog</Code> holds one row per product, with a
            few prices changing each run. <Code>priced</Code> joins them, and <Code>revenue</Code> totals{' '}
            <Code>priced</Code> by product. Every square is one row. Start with the first run, then keep running.
          </p>
          <Pipeline />
          <p style={{ ...para, color: FAINT, fontSize: 13 }}>
            The tables are kept small so every row fits on screen. In the Trickle demo, <Code>orders</Code> starts at 1.5
            million rows and grows by 25,000 a run, and <Code>catalog</Code> has 100,000 products with 100 price changes a
            run, so the share of rows that take part is far smaller again.
          </p>
        </Section>

        <footer style={{ borderTop: '1px solid #34333a', paddingTop: 18, display: 'flex', flexWrap: 'wrap', gap: 18, fontFamily: MONO, fontSize: 13 }}>
          <a href={DOCS} target="_blank" rel="noopener noreferrer" style={link}>Trickles in the docs ↗</a>
          <Link href="/" style={link}>Pull orchestration in the Playground</Link>
        </footer>
      </div>
    </main>
  );
}

function Section({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section style={{ marginBottom: 48, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <div style={{ fontFamily: MONO, fontSize: 11, color: THEME_BRAND, letterSpacing: '0.12em', marginBottom: 4 }}>
          {String(n).padStart(2, '0')}
        </div>
        <h2 style={{ fontSize: 22, margin: 0, fontWeight: 650 }}>{title}</h2>
      </div>
      {children}
    </section>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code style={{ fontFamily: MONO, fontSize: '0.9em', background: '#2a292e', padding: '1px 5px', borderRadius: 4, whiteSpace: 'nowrap' }}>
      {children}
    </code>
  );
}

const lead: React.CSSProperties = { fontSize: 17, lineHeight: 1.6, color: MUTED, margin: 0 };
const para: React.CSSProperties = { fontSize: 15, lineHeight: 1.65, color: MUTED, margin: 0 };
const link: React.CSSProperties = { color: THEME_BRAND, textDecoration: 'none' };
