'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLiveStore } from '@/lib/store';
import { fetchTables, fetchCount, fetchPage, type DataQuery, UnauthorizedError } from '@/lib/api';
import type { PondId } from '@/lib/types';

const ROW_H = 26; // fixed row height — the virtual-scroll math depends on it
const NUM_W = 60; // row-number column width
const COL_W = 180; // data column width (table-layout: fixed, so columns are exact and aligned)
const CHUNK = 400; // rows held in memory / fetched per window
const OVERSCAN = 80; // rows of slack before the visible range reaches a loaded-window edge

const browseSql = (pond: string, table: string) => `SELECT * FROM "${pond}"."${table}" LIMIT 1000`;
const on401 = (e: unknown) => e instanceof UnauthorizedError && useLiveStore.setState({ needsKey: true });

// Open the viewer only while a Pond is targeted, and key it by that id so each open is a fresh mount.
export function DataViewerModal() {
  const pondId = useLiveStore((s) => s.dataViewerPondId);
  if (!pondId) return null;
  return <DataViewer key={pondId} pondId={pondId} />;
}

// Full-screen viewer for a Pond's exported tables. A table dropdown + editable SQL box sit over a
// windowed virtual grid: only the visible slice of rows is fetched (by row offset), so an arbitrarily
// large table never lands in browser memory. Running custom SQL switches to query mode; Clear returns
// to browsing the selected table.
function DataViewer({ pondId }: { pondId: PondId }) {
  const close = useLiveStore((s) => s.closeDataViewer);
  const pondName = useLiveStore((s) => s.ponds[pondId]?.name ?? pondId);

  const [tables, setTables] = useState<string[] | null>(null); // null = still loading
  const [table, setTable] = useState<string | null>(null);
  const [mode, setMode] = useState<'browse' | 'query'>('browse');
  const [sqlText, setSqlText] = useState('');
  const [activeSql, setActiveSql] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [tablesError, setTablesError] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // The SQL box grows on focus and collapses when it loses focus; while focused it can be dragged
  // taller. Height is driven imperatively (never via React-managed `height`) so a re-render from
  // typing — or React reconciliation — can't clobber a height the user dragged.
  const expand = () => {
    setExpanded(true);
    const ta = taRef.current;
    if (ta && ta.offsetHeight < 110) ta.style.height = '110px';
  };
  const collapse = () => {
    setExpanded(false);
    if (taRef.current) taRef.current.style.height = '38px';
  };

  // The active data source: a named table (browse) or the last-run SQL (query).
  const query: DataQuery | null =
    mode === 'query' ? { pond: pondId, sql: activeSql } : table ? { pond: pondId, table } : null;
  // Remounts the grid (and resets the header count) whenever the source changes.
  const queryKey = mode === 'query' ? `sql:${activeSql}` : `tbl:${table ?? ''}`;

  // Load the Pond's table list once on mount (deferred so no setState runs synchronously in the effect).
  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        const ts = await fetchTables(pondId);
        setTables(ts);
        if (ts.length) {
          setTable(ts[0]);
          setSqlText(browseSql(pondName, ts[0]));
        }
      } catch (e) {
        on401(e);
        setTablesError(e instanceof Error ? e.message : String(e));
        setTables([]);
      }
    }, 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Esc closes the viewer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [close]);

  const selectTable = (t: string) => {
    setTable(t);
    setMode('browse');
    setSqlText(browseSql(pondName, t));
    setTotal(null);
  };
  const runQuery = () => {
    if (!sqlText.trim()) return;
    setActiveSql(sqlText);
    setMode('query');
    setTotal(null);
  };
  const clearQuery = () => {
    setMode('browse');
    setTotal(null);
    if (table) setSqlText(browseSql(pondName, table));
  };

  return (
    <div
      onClick={close}
      style={{
        position: 'fixed', inset: 0, zIndex: 1100, display: 'flex', alignItems: 'center',
        justifyContent: 'center', background: 'rgba(9, 9, 11, 0.78)', backdropFilter: 'blur(2px)',
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#101014', border: '1px solid #27272a', borderRadius: 10,
          width: '92vw', height: '88vh', display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        {/* Header: pond · table picker · row count · close */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px', borderBottom: '1px solid #27272a', flexShrink: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7' }}>{pondName}</span>
          {tables && tables.length > 0 && (
            // The closed control is fully themed (appearance:none + a custom caret); the open option
            // list stays the platform-native picker, which is the right call on mobile.
            <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
              <select
                value={mode === 'browse' ? table ?? '' : ''}
                onChange={(e) => selectTable(e.target.value)}
                style={{
                  appearance: 'none', WebkitAppearance: 'none', MozAppearance: 'none',
                  background: '#18181b', border: '1px solid #3f3f46', borderRadius: 6, padding: '5px 26px 5px 9px',
                  color: '#e4e4e7', fontSize: 12.5, fontFamily: 'inherit', outline: 'none', cursor: 'pointer',
                }}
              >
                {mode === 'query' && <option value="">(query)</option>}
                {tables.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
              <span style={{ position: 'absolute', right: 9, pointerEvents: 'none', color: '#71717a', fontSize: 9 }}>▼</span>
            </span>
          )}
          {mode === 'query' && (
            <span style={{ fontSize: 10, fontWeight: 700, color: '#ee9333', letterSpacing: '0.06em' }}>QUERY</span>
          )}
          <span style={{ fontSize: 11, color: '#71717a' }}>
            {total == null ? '' : `${total.toLocaleString()} row${total === 1 ? '' : 's'}`}
          </span>
          <button
            onClick={close}
            title="Close (Esc)"
            style={{
              marginLeft: 'auto', background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5,
              color: '#a1a1aa', fontSize: 13, lineHeight: 1, padding: '4px 9px', cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            ✕
          </button>
        </div>

        {/* SQL box */}
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '10px 14px', borderBottom: '1px solid #27272a', flexShrink: 0 }}>
          <textarea
            ref={taRef}
            value={sqlText}
            onChange={(e) => setSqlText(e.target.value)}
            onFocus={expand}
            onBlur={collapse}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                runQuery();
              }
            }}
            spellCheck={false}
            style={{
              flex: 1, resize: expanded ? 'vertical' : 'none', height: 38, minHeight: 38, maxHeight: '45vh',
              background: '#18181b', border: '1px solid #3f3f46', borderRadius: 6, padding: '8px 10px',
              color: '#e4e4e7', fontSize: 12.5, fontFamily: 'inherit', outline: 'none', lineHeight: 1.5,
            }}
          />
          <div style={{ display: 'flex', gap: 6 }}>
            <button
              onClick={runQuery}
              title="Run (⌘/Ctrl+Enter)"
              style={{
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center', height: 38, padding: '0 16px',
                background: '#06c4e6', border: 'none', borderRadius: 6,
                color: '#09090b', fontSize: 12.5, fontWeight: 700, cursor: 'pointer', fontFamily: 'inherit',
              }}
            >
              Run
            </button>
            {mode === 'query' && (
              <button
                onClick={clearQuery}
                title="Clear query — back to browsing the table"
                style={{
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center', height: 38, padding: '0 14px',
                  background: 'transparent', border: '1px solid #3f3f46', borderRadius: 6,
                  color: '#a1a1aa', fontSize: 12.5, fontWeight: 700, cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {/* Grid */}
        <div style={{ flex: 1, minHeight: 0 }}>
          {tablesError ? (
            <div style={{ padding: 16, color: '#ef4444', fontSize: 12.5 }}>{tablesError}</div>
          ) : tables == null ? (
            <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>
          ) : query == null ? (
            <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>This Pond has no exported tables.</div>
          ) : (
            <VirtualGrid key={queryKey} query={query} onTotal={setTotal} />
          )}
        </div>
      </div>
    </div>
  );
}

// A windowed virtual grid: sizes its scroll to `total` rows, but only ever holds (and fetches) the
// rows around the viewport — scrolling away drops them, scrolling back re-fetches. Stable because the
// underlying Parquet scan order is deterministic.
function VirtualGrid({ query, onTotal }: { query: DataQuery; onTotal: (n: number) => void }) {
  const [total, setTotal] = useState<number | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [windowStart, setWindowStart] = useState(0);
  const [rows, setRows] = useState<unknown[][]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const reqId = useRef(0); // newest window-fetch token; a stale resolve is ignored
  const pendingStart = useRef(-1); // window start currently in flight (dedupes scroll-triggered fetches)
  const loaded = useRef({ start: 0, end: 0 }); // the loaded row range [start, end)

  const fetchWindow = useCallback(
    async (start: number) => {
      if (start === pendingStart.current) return;
      pendingStart.current = start;
      const id = ++reqId.current;
      try {
        const res = await fetchPage({ ...query, limit: CHUNK, offset: start });
        if (id !== reqId.current) return; // superseded by a newer fetch
        setColumns((prev) => (res.columns.length ? res.columns : prev));
        setRows(res.rows);
        setWindowStart(start);
        loaded.current = { start, end: start + res.rows.length };
      } catch (e) {
        if (id !== reqId.current) return;
        on401(e);
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (id === reqId.current) pendingStart.current = -1;
      }
    },
    [query]
  );

  // On mount: total row count (sizes the scroll), then the first window.
  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        const c = await fetchCount(query);
        setTotal(c);
        onTotal(c);
        if (c > 0) await fetchWindow(0);
      } catch (e) {
        on401(e);
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    }, 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el || total == null) return;
    const firstVisible = Math.floor(el.scrollTop / ROW_H);
    const lastVisible = firstVisible + Math.ceil(el.clientHeight / ROW_H);
    const { start, end } = loaded.current;
    const needAbove = start > 0 && firstVisible < start + OVERSCAN;
    const needBelow = end < total && lastVisible > end - OVERSCAN;
    if (needAbove || needBelow) {
      const newStart = Math.max(0, Math.min(firstVisible - OVERSCAN, Math.max(0, total - CHUNK)));
      if (newStart !== start) void fetchWindow(newStart);
    }
  };

  if (error) return <div style={{ padding: 16, color: '#ef4444', fontSize: 12.5, whiteSpace: 'pre-wrap' }}>{error}</div>;
  if (loading) return <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>;
  if (total === 0 || columns.length === 0) return <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>No rows.</div>;

  const topPad = windowStart * ROW_H;
  const botPad = Math.max(0, ((total ?? 0) - windowStart - rows.length) * ROW_H);
  const tableWidth = NUM_W + columns.length * COL_W;

  return (
    <div ref={scrollRef} onScroll={onScroll} style={{ height: '100%', overflow: 'auto' }}>
      <table style={{ tableLayout: 'fixed', borderCollapse: 'collapse', width: tableWidth, fontSize: 12, color: '#d4d4d8' }}>
        <colgroup>
          <col style={{ width: NUM_W }} />
          {columns.map((c) => (
            <col key={c} style={{ width: COL_W }} />
          ))}
        </colgroup>
        <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
          <tr>
            <th style={th({ color: '#52525b', textAlign: 'right' })}>#</th>
            {columns.map((c) => (
              <th key={c} style={th({})} title={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {topPad > 0 && (
            <tr style={{ height: topPad }}>
              <td colSpan={columns.length + 1} style={{ padding: 0, border: 0 }} />
            </tr>
          )}
          {rows.map((row, i) => {
            const idx = windowStart + i;
            return (
              <tr key={idx} style={{ height: ROW_H, background: idx % 2 ? '#121217' : 'transparent' }}>
                <td style={td({ color: '#52525b', textAlign: 'right' })}>{idx + 1}</td>
                {row.map((v, j) => (
                  <td key={j} style={td({})} title={v == null ? '' : String(v)}>
                    {v == null ? <span style={{ color: '#3f3f46' }}>·</span> : String(v)}
                  </td>
                ))}
              </tr>
            );
          })}
          {botPad > 0 && (
            <tr style={{ height: botPad }}>
              <td colSpan={columns.length + 1} style={{ padding: 0, border: 0 }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function th(extra: React.CSSProperties): React.CSSProperties {
  return {
    textAlign: 'left', padding: '0 12px', height: ROW_H, lineHeight: `${ROW_H}px`,
    borderBottom: '1px solid #3f3f46', background: '#1a1a1f', color: '#a1a1aa', fontWeight: 700,
    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', ...extra,
  };
}

function td(extra: React.CSSProperties): React.CSSProperties {
  return {
    padding: '0 12px', height: ROW_H, lineHeight: `${ROW_H}px`,
    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', ...extra,
  };
}
