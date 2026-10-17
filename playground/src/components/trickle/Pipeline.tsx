'use client';

import { useRef, useState } from 'react';
import { THEME_BRAND } from '@/lib/store';
import { TrickleSim, type RunEvents, type RunResult, type TableView } from '@/lib/trickle/pipeline';
import { CellGrid } from './CellGrid';
import { pill } from './ZSetFigure';
import { BORDER, FAINT, Legend, MONO, MUTED, PANEL, TEXT, type CellMark } from './theme';

// The demo pipeline, run by run. Every square is a row. A run lights up the rows it added, changed or removed,
// and the rows that took part in computing them; the next run first folds those back into the grey state.

const FOLD_MS = 450;

const PONDS: { key: 'orders' | 'catalog' | 'priced' | 'revenue'; name: string; what: string }[] = [
  { key: 'orders', name: 'orders', what: 'order lines, appended each run' },
  { key: 'catalog', name: 'catalog', what: 'one row per product, prices change' },
  { key: 'priced', name: 'priced', what: 'orders joined to catalog' },
  { key: 'revenue', name: 'revenue', what: 'priced, totalled per product' },
];

const EVENT_LABELS: { key: keyof RunEvents; label: string }[] = [
  { key: 'newOrders', label: 'New orders' },
  { key: 'reprice', label: 'Price changes' },
  { key: 'discontinue', label: 'Remove a product' },
];

function marksOf(view: TableView, folded: boolean): CellMark[] {
  if (!folded) return view.ids.map((id) => view.marks.get(id) ?? 'base');
  return view.ids.filter((id) => view.marks.get(id) !== 'removed').map(() => 'base');
}

function counts(view: TableView, bootstrap: boolean): string {
  if (bootstrap) return `${view.size.toLocaleString()} rows, all new`;
  const parts = [`${view.size.toLocaleString()} rows`];
  if (view.added) parts.push(`${view.added} added`);
  if (view.updated) parts.push(`${view.updated} changed`);
  if (view.removed) parts.push(`${view.removed} removed`);
  if (view.read) parts.push(`${view.read} took part`);
  return parts.join(' · ');
}

export function Pipeline() {
  const sim = useRef<TrickleSim | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [folded, setFolded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<RunEvents>({ newOrders: true, reprice: true, discontinue: false });

  const run = () => {
    if (busy) return;
    if (!sim.current) sim.current = new TrickleSim();
    const next = () => {
      setResult(sim.current!.step(events));
      setFolded(false);
      setBusy(false);
    };
    const reduce = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (!result || reduce) return next();
    setBusy(true);
    setFolded(true);
    window.setTimeout(next, FOLD_MS);
  };

  const reset = () => {
    sim.current = null;
    setResult(null);
    setFolded(false);
  };

  const nothingChosen = !!result && !events.newOrders && !events.reprice && !events.discontinue;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8, padding: 14, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
          <button data-testid="trickle-run" onClick={run} disabled={busy} style={{
            background: THEME_BRAND, color: '#0f0f14', border: 'none', borderRadius: 6, padding: '8px 16px',
            fontFamily: MONO, fontSize: 13, fontWeight: 700, cursor: busy ? 'default' : 'pointer', minHeight: 36,
          }}>
            {result ? `Run ${result.run + 1}` : 'First run'}
          </button>
          {result && (
            <button onClick={reset} style={pill(false)}>Start again</button>
          )}
          <span data-testid="trickle-run-label" style={{ marginLeft: 'auto', fontFamily: MONO, fontSize: 12, color: FAINT }}>
            {result ? `after run ${result.run}` : 'not run yet'}
          </span>
        </div>
        {result && (
          <div>
            <div style={{ fontSize: 12, color: FAINT, marginBottom: 6 }}>Next run brings</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {EVENT_LABELS.map(({ key, label }) => (
                <button key={key} role="switch" aria-checked={events[key]} onClick={() => setEvents((e) => ({ ...e, [key]: !e[key] }))}
                  style={pill(events[key])}>
                  {events[key] ? '✓ ' : ''}{label}
                </button>
              ))}
            </div>
            {nothingChosen && <div style={{ fontSize: 12, color: FAINT, marginTop: 6 }}>With no changes, the run has nothing to do.</div>}
          </div>
        )}
        <Legend marks={['base', 'read', 'added', 'updated', 'removed']} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3" style={{ gap: 14 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {PONDS.slice(0, 2).map((p) => <PondCard key={p.key} pond={p} result={result} folded={folded} />)}
        </div>
        <PondCard pond={PONDS[2]} result={result} folded={folded} arrow />
        <PondCard pond={PONDS[3]} result={result} folded={folded} arrow />
      </div>

      <Summary result={result} folded={folded} />
    </div>
  );
}

function PondCard({ pond, result, folded, arrow }: {
  pond: (typeof PONDS)[number]; result: RunResult | null; folded: boolean; arrow?: boolean;
}) {
  const view = result?.[pond.key];
  return (
    <div data-testid={`pond-${pond.key}`} style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8, padding: 12, alignSelf: 'start' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        {arrow && <span aria-hidden style={{ color: FAINT, fontFamily: MONO }}>→</span>}
        <span style={{ fontFamily: MONO, fontSize: 14, color: TEXT, fontWeight: 600 }}>{pond.name}</span>
        <span style={{ fontSize: 12, color: FAINT }}>{pond.what}</span>
      </div>
      <div style={{ marginTop: 10, minHeight: 24 }}>
        {view ? (
          <CellGrid marks={marksOf(view, folded)} label={`${pond.name}: ${counts(view, result!.bootstrap)}`} />
        ) : (
          <div style={{ fontSize: 12, color: FAINT }}>empty</div>
        )}
      </div>
      <div data-testid={`pond-${pond.key}-counts`} style={{ fontFamily: MONO, fontSize: 11, color: MUTED, marginTop: 8, minHeight: 16 }}>
        {view && !folded ? counts(view, result!.bootstrap) : ' '}
      </div>
    </div>
  );
}

function Summary({ result, folded }: { result: RunResult | null; folded: boolean }) {
  let text: React.ReactNode;
  if (!result) {
    text = 'The first run reads every source in full, as there is nothing earlier to build on.';
  } else if (folded) {
    text = 'Folding the last run into the current state…';
  } else if (result.bootstrap) {
    text = `Run 1 computed everything: priced joined all ${result.joinFull.toLocaleString()} rows of orders and catalog, and revenue totalled all ${result.aggFull.toLocaleString()} priced rows. From here on, each run starts from these tables and their changes.`;
  } else {
    text = (
      <>
        <b style={{ color: TEXT }}>priced:</b> {result.joinRows.toLocaleString()} rows of orders and catalog took part, of the{' '}
        {result.joinFull.toLocaleString()} a full recompute would join.{' '}
        <b style={{ color: TEXT }}>revenue:</b> {result.aggRows.toLocaleString()} rows took part (priced changes and the totals they
        update), of the {result.aggFull.toLocaleString()} priced rows a full recompute would read.
      </>
    );
  }
  return (
    <p data-testid="trickle-summary" style={{ margin: 0, fontSize: 14, color: MUTED, lineHeight: 1.55 }}>
      {text}
    </p>
  );
}
