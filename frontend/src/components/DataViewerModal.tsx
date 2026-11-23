'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLiveStore, atLeast, THEME_BLOCKED, THEME_BRAND, THEME_SUCCESS } from '@/lib/store';
import {
  fetchTables, fetchFreshness, fetchHistory, fetchCount, fetchPage, cancelQuery, fetchObjects, downloadObject,
  deleteTable, deleteObject, fetchCatalog, promoteServe, exposeTable,
  fetchAppData, fetchMedia, downloadMedia, uploadMedia, deleteMedia,
  type DataQuery, type TableInfo, type TrickleMode, type PageResult, type ObjectInfo,
  type Catalog, type CatalogTable, type AppDataLine, type MediaFile, UnauthorizedError,
} from '@/lib/api';
import { ConfirmDialog, type ConfirmOpts } from './ConfirmDialog';
import { useIsMobile, useIsShort } from '@/lib/useIsMobile';

// The unified Catalog (plans/data-serving.md): one modal that IS the Data Viewer. Opened from Options →
// Catalog (no preselection) or a Pond's table icon (that Pond+Major preselected). A collapsible left
// pond-tree (per-pond version dropdown + expandable table list with exposure eyes + copy) drives the
// embedded Data Viewer on the right. The table dropdown/browse stays single-pond + Trickle-aware; the
// hand-written SQL box runs cross-pond through the serving core (governed: read/demand see only the
// serviceable surface; full manages exposure + the served-major pointer).

const ROW_H = 26;
const NUM_W = 60;
const COL_W = 180;
const CHUNK = 400;
const OVERSCAN = 80;

// Trickle system columns surfaced by the consolidated browse view.
const ACTIVE = '_duckstring_active'; // 1 current / 0 replaced / -1 deleted — drives row colour, never shown
const SLOW_SORT_ROWS = 100_000_000; // a sort past this many rows asks first
const FRESH = '_duckstring_f'; // most-recent run freshness
const UPDATES = '_duckstring_updates'; // count of +1 changelog events
const EVENT = '_duckstring_event'; // create | update | delete (per-record history)
const DELTA = '_duckstring_d'; // Z-set weight: +1 insert / -1 retraction — shown on raw __changelog views
const COL_LABELS: Record<string, string> = { [FRESH]: 'freshness', [UPDATES]: 'updates', [EVENT]: 'event', [DELTA]: 'Δ' };
// History event → label colour (reusing the theme): create = white, update = brand cyan, delete = blocked red.
const EVENT_COLOR: Record<string, string> = { create: '#f4f4f5', update: THEME_BRAND, delete: THEME_BLOCKED };

// A Trickle companion (X__changelog / X__band / X__droplog / X__base) belongs to base table X — a delete
// takes the whole collection, so a companion resolves to its base (mirrors trickle_io.base_table_name).
function baseTableName(name: string): string {
  for (const s of ['__changelog', '__band', '__droplog', '__base']) {
    if (name.endsWith(s) && name.length > s.length) return name.slice(0, -s.length);
  }
  return name;
}

// The serving-core schema for a (pond, major): the bare pond name for the served major, `{pond}_v{major}`
// otherwise — so a browsed non-served major's default SQL targets the right line when Run'd cross-pond.
const schemaName = (pond: string, major: number, served: number | null) =>
  major === served ? pond : `${pond}_v${major}`;
const browseSql = (pond: string, major: number, served: number | null, table: string) =>
  `SELECT * FROM "${schemaName(pond, major, served)}"."${table}" LIMIT 1000`;
// The copy-to-clipboard query name ALWAYS carries the _vN suffix (robust against a later promotion);
// the bare `pond.table` form is only a convenience for exploring through a connected app.
const copyName = (pond: string, major: number, table: string) => `${pond}_v${major}.${table}`;

const on401 = (e: unknown) => e instanceof UnauthorizedError && useLiveStore.setState({ needsKey: true });
// A freshness ISO → compact, stable 'YYYY-MM-DD HH:MM:SS' (backend serialises in UTC).
const fmtTs = (iso: string) => iso.slice(0, 19).replace('T', ' ');

// A pond id "name@major" → its parts (the tree preselects from the store's dataViewerPondId).
function splitId(id: string): { name: string; major: number | null } {
  const at = id.lastIndexOf('@');
  return { name: at === -1 ? id : id.slice(0, at), major: at === -1 ? null : Number(id.slice(at + 1)) };
}

type Pending = { pond: string; major: number; table: string; nonce: number };
// An App line's data picked in the tree (plans/records-and-media.md): its Records tables or its Media.
type AppSel = { name: string; major: number; view: 'records' | 'media'; table?: string };

// ─── The modal shell: tree + viewer ─────────────────────────────────────────

export function DataViewerModal() {
  const catalogOpen = useLiveStore((s) => s.catalogOpen);
  const preselect = useLiveStore((s) => s.dataViewerPondId);
  const setCatalogOpen = useLiveStore((s) => s.setCatalogOpen);
  const closeDataViewer = useLiveStore((s) => s.closeDataViewer);
  if (!catalogOpen && !preselect) return null;
  return <CatalogModal preselect={preselect} onClose={() => { setCatalogOpen(false); closeDataViewer(); }} />;
}

function CatalogModal({ preselect, onClose }: { preselect: string | null; onClose: () => void }) {
  const canManage = useLiveStore((s) => atLeast(s.accessLevel, 'full'));
  const [cat, setCat] = useState<Catalog | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [treeMajor, setTreeMajor] = useState<Record<string, number>>({});
  const [search, setSearch] = useState('');
  // Opened from a Pond (preselect) → start focused on that Pond (tree collapsed); from Options → Catalog
  // → start with the tree open to browse.
  const [leftCollapsed, setLeftCollapsed] = useState(!!preselect);
  const [pending, setPending] = useState<Pending | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [appLines, setAppLines] = useState<AppDataLine[]>([]);
  const [appSel, setAppSel] = useState<AppSel | null>(null);
  const nonce = useRef(1);

  const load = useCallback(async () => {
    const pre = preselect ? splitId(preselect) : null;
    try {
      const data = await fetchAppData();
      setAppLines(data.lines);
      // Opened from an App node's table icon: start on that line's data.
      const line = pre && data.lines.find((l) => l.name === pre.name && l.major === pre.major);
      if (line) setAppSel((a) => a ?? { name: line.name, major: line.major, view: line.records ? 'records' : 'media' });
    } catch (e) {
      on401(e);
    }
    try {
      const c = await fetchCatalog();
      setCat(c);
      setActive((a) => a ?? pre?.name ?? c.ponds[0]?.name ?? null);
      if (pre?.major != null) setTreeMajor((m) => (pre.name in m ? m : { ...m, [pre.name]: pre.major as number }));
    } catch (e) {
      on401(e);
      setCat({ catchment: null, ponds: [], connect: {} });
    }
  }, [preselect]);
  useEffect(() => {
    const t = setTimeout(() => void load(), 0); // defer — no synchronous setState in the effect body
    return () => clearTimeout(t);
  }, [load]);

  const activePond = cat?.ponds.find((p) => p.name === active) ?? null;
  const servedMajor = activePond?.served_major ?? null;
  const activeMajor = activePond ? (treeMajor[activePond.name] ?? activePond.served_major) : null;
  const matchesActive = (p: Pending | null) =>
    !!p && activePond != null && p.pond === activePond.name && p.major === activeMajor;

  const pickTable = (pond: string, major: number, table: string) => {
    setAppSel(null);
    setActive(pond);
    setTreeMajor((m) => ({ ...m, [pond]: major }));
    setPending({ pond, major, table, nonce: nonce.current++ });
  };
  const onExpose = (pond: string, major: number, t: CatalogTable) =>
    exposeTable(pond, t.table, !t.exposed, major).then(load).catch((e) => setErr(String(e?.message ?? e)));
  const onPromote = () =>
    activePond && activeMajor != null
      ? promoteServe(activePond.name, activeMajor).then(load).catch((e) => setErr(String(e?.message ?? e)))
      : undefined;

  return (
    <div
      onClick={onClose}
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
          width: '94vw', height: '90vh', display: 'flex', overflow: 'hidden', position: 'relative',
        }}
      >
        {/* Left: the collapsible pond tree. */}
        {!leftCollapsed && (
          <div style={{ width: 248, flexShrink: 0, borderRight: '1px solid #27272a', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <PondTree
              cat={cat}
              active={active}
              treeMajor={treeMajor}
              search={search}
              setSearch={setSearch}
              canManage={canManage}
              onActivate={(name) => { setAppSel(null); setActive(name); }}
              appLines={appLines}
              appSel={appSel}
              onPickApp={setAppSel}
              onPickMajor={(name, major) => setTreeMajor((m) => ({ ...m, [name]: major }))}
              onPickTable={pickTable}
              onExpose={onExpose}
            />
            <ConnectFooter catchment={cat?.catchment ?? null} connect={cat?.connect ?? {}} />
          </div>
        )}

        {/* Right: an App line's data, or the Data Viewer keyed on pond+major so a switch remounts it fresh. */}
        {appSel && appLines.some((l) => l.name === appSel.name && l.major === appSel.major) ? (
          <AppDataViewer
            key={`${appSel.name}:${appSel.major}`}
            line={appLines.find((l) => l.name === appSel.name && l.major === appSel.major)!}
            sel={appSel}
            onSelect={setAppSel}
            canManage={canManage}
            leftCollapsed={leftCollapsed}
            onToggleLeft={() => setLeftCollapsed((v) => !v)}
            onChanged={load}
            onClose={onClose}
          />
        ) : activePond && activeMajor != null ? (
          <DataViewer
            key={`${activePond.name}:${activeMajor}`}
            pondName={activePond.name}
            major={activeMajor}
            servedMajor={servedMajor}
            canManage={canManage}
            initialTable={matchesActive(pending) ? pending!.table : undefined}
            selectSig={matchesActive(pending) ? pending : null}
            leftCollapsed={leftCollapsed}
            onToggleLeft={() => setLeftCollapsed((v) => !v)}
            onPromote={onPromote}
            onClose={onClose}
          />
        ) : (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#71717a', fontSize: 13, position: 'relative' }}>
            {leftCollapsed && (
              <button onClick={() => setLeftCollapsed(false)} title="Show ponds"
                      style={{ position: 'absolute', top: 12, left: 12, ...chip }}>›</button>
            )}
            {cat == null ? 'Loading…' : 'Nothing serviceable.'}
            <button onClick={onClose} style={{ position: 'absolute', top: 12, right: 12, ...chip }}>✕</button>
          </div>
        )}

        {err && (
          <div onClick={() => setErr(null)} title="dismiss"
               style={{ position: 'absolute', bottom: 12, right: 12, maxWidth: '50%', background: '#2a1416',
                        border: `1px solid ${THEME_BLOCKED}66`, borderRadius: 6, padding: '6px 10px',
                        color: '#fca5a5', fontSize: 11.5, cursor: 'pointer', wordBreak: 'break-word' }}>
            {err}
          </div>
        )}
      </div>
    </div>
  );
}

const chip: React.CSSProperties = {
  background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5, color: '#a1a1aa',
  fontSize: 13, lineHeight: 1, padding: '4px 9px', cursor: 'pointer', fontFamily: 'inherit',
};

// ─── The pond tree (left panel) ──────────────────────────────────────────────

function PondTree({
  cat, active, treeMajor, search, setSearch, canManage, onActivate, onPickMajor, onPickTable, onExpose,
  appLines, appSel, onPickApp,
}: {
  appLines: AppDataLine[];
  appSel: AppSel | null;
  onPickApp: (s: AppSel) => void;
  cat: Catalog | null;
  active: string | null;
  treeMajor: Record<string, number>;
  search: string;
  setSearch: (s: string) => void;
  canManage: boolean;
  onActivate: (name: string) => void;
  onPickMajor: (name: string, major: number) => void;
  onPickTable: (pond: string, major: number, table: string) => void;
  onExpose: (pond: string, major: number, t: CatalogTable) => void;
}) {
  const ponds = (cat?.ponds ?? []).filter(
    (p) => !search || p.name.includes(search) || p.tables.some((t) => t.table.includes(search)));
  return (
    <>
      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="search ponds / tables"
        style={{
          margin: 8, boxSizing: 'border-box', width: 'calc(100% - 16px)', background: '#18181b',
          border: '1px solid #3f3f46', borderRadius: 6, color: '#e4e4e7', padding: '6px 9px',
          fontSize: 12, fontFamily: 'inherit', outline: 'none',
        }}
      />
      <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
        {ponds.map((p) => {
          const isActive = p.name === active;
          const major = treeMajor[p.name] ?? p.served_major;
          const tables = p.tables.filter((t) => t.major === major);
          // While searching, list only the matching tables; otherwise the whole (selected-major) set.
          const shown = search ? tables.filter((t) => t.table.includes(search)) : tables;
          // Expand the active pond, and (while searching) any pond that holds a matching table.
          const showTables = isActive || (search !== '' && p.tables.some((t) => t.table.includes(search)));
          return (
            <div key={p.name}>
              <div
                onClick={() => onActivate(p.name)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', cursor: 'pointer',
                  fontSize: 12.5, background: isActive ? '#1f1f27' : 'transparent',
                  color: isActive ? '#f4f4f5' : '#c4c4c8',
                }}
              >
                <span style={{ color: '#71717a', fontSize: 10, width: 8 }}>{showTables ? '▾' : '▸'}</span>
                <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.name}
                </span>
                <select
                  value={major}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => onPickMajor(p.name, Number(e.target.value))}
                  title="Major line — the served major is green; determines the tables shown below"
                  style={{
                    appearance: 'none', WebkitAppearance: 'none', MozAppearance: 'none',
                    background: '#18181b', border: '1px solid #3f3f46', borderRadius: 5, padding: '2px 6px',
                    color: major === p.served_major ? THEME_SUCCESS : '#a1a1aa', fontSize: 11,
                    fontFamily: 'inherit', outline: 'none', cursor: 'pointer',
                  }}
                >
                  {p.majors.map((m) => (
                    <option key={m} value={m}
                            style={{ color: m === p.served_major ? THEME_SUCCESS : undefined,
                                     fontWeight: m === p.served_major ? 700 : 400 }}>
                      v{m}
                    </option>
                  ))}
                </select>
              </div>
              {showTables && (
                <div style={{ padding: '2px 0 6px 0' }}>
                  {shown.map((t) => (
                    <TableRow key={t.table} pond={p.name} major={major} t={t} canManage={canManage}
                              onPick={() => onPickTable(p.name, major, t.table)}
                              onExpose={() => onExpose(p.name, major, t)} />
                  ))}
                  {shown.length === 0 && (
                    <div style={{ padding: '4px 10px 4px 24px', color: '#52525b', fontSize: 11.5 }}>
                      {search ? 'No matching tables.' : `No ${canManage ? '' : 'serviceable '}tables on v${major}.`}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
        {ponds.length === 0 && (
          <div style={{ padding: 12, color: '#52525b', fontSize: 12 }}>
            {cat == null ? 'Loading…' : 'Nothing serviceable.'}
          </div>
        )}
        <AppDataTree lines={appLines} sel={appSel} search={search} onPick={onPickApp} />
      </div>
    </>
  );
}

// Apps' Records and Media, a section of their own below the Ponds: each App line, its Records tables and its Media.
function AppDataTree({ lines, sel, search, onPick }: {
  lines: AppDataLine[]; sel: AppSel | null; search: string; onPick: (s: AppSel) => void;
}) {
  const shown = lines.filter((l) => !search || l.name.includes(search) || (l.tables ?? []).some((t) => t.table.includes(search)));
  if (shown.length === 0) return null;
  const item = (active: boolean): React.CSSProperties => ({
    display: 'flex', alignItems: 'center', gap: 6, padding: '3px 10px 3px 22px', cursor: 'pointer', fontSize: 12,
    color: active ? '#f4f4f5' : '#c4c4c8', background: active ? '#1f1f27' : 'transparent',
  });
  return (
    <div style={{ borderTop: '1px solid #27272a', marginTop: 6, paddingTop: 6 }}>
      <div style={{ padding: '4px 10px', fontSize: 10.5, color: '#71717a', letterSpacing: 0.4, textTransform: 'uppercase' }}>
        App data
      </div>
      {shown.map((l) => {
        const isLine = sel?.name === l.name && sel?.major === l.major;
        const tables = (l.tables ?? []).filter((t) => !search || t.table.includes(search) || l.name.includes(search));
        return (
          <div key={`${l.name}@${l.major}`}>
            <div onClick={() => onPick({ name: l.name, major: l.major, view: l.records ? 'records' : 'media' })}
                 style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', cursor: 'pointer', fontSize: 12.5,
                          color: isLine ? '#f4f4f5' : '#c4c4c8' }}>
              <span style={{ color: '#71717a', fontSize: 10, width: 8 }}>{isLine ? '▾' : '▸'}</span>
              <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {l.name} <span style={{ color: '#71717a' }}>v{l.major}</span>
              </span>
              {l.state !== 'live' && <span style={{ fontSize: 10.5, color: '#71717a' }}>{l.state}</span>}
            </div>
            {isLine && (
              <div style={{ padding: '2px 0 6px 0' }}>
                {l.records && tables.map((t) => (
                  <div key={t.table} onClick={() => onPick({ name: l.name, major: l.major, view: 'records', table: t.table })}
                       title={`records.${l.schema}.${t.table}`}
                       style={item(sel?.view === 'records' && sel?.table === t.table)}>
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.table}</span>
                  </div>
                ))}
                {l.records && tables.length === 0 && (
                  <div style={{ padding: '4px 10px 4px 24px', color: '#52525b', fontSize: 11.5 }}>No Records tables yet.</div>
                )}
                {l.media && (
                  <div onClick={() => onPick({ name: l.name, major: l.major, view: 'media' })} style={item(sel?.view === 'media')}>
                    <span style={{ flex: 1 }}>Media</span>
                    {l.usage && <span style={{ color: '#71717a', fontSize: 11 }}>{l.usage.files}</span>}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── An App line's data (right panel) ────────────────────────────────────────

function AppDataViewer({ line, sel, onSelect, canManage, leftCollapsed, onToggleLeft, onChanged, onClose }: {
  line: AppDataLine;
  sel: AppSel;
  onSelect: (s: AppSel) => void;
  canManage: boolean;
  leftCollapsed: boolean;
  onToggleLeft: () => void;
  onChanged: () => void;
  onClose: () => void;
}) {
  const table = sel.view === 'records' ? (sel.table ?? line.tables?.[0]?.table) : undefined;
  const [sort, setSort] = useState<{ col: string | null; desc: boolean }>({ col: null, desc: false });
  const [runSeq, setRunSeq] = useState(0);
  const [, setTotal] = useState<number | null>(null);
  const cycleSort = (col: string) =>
    setSort((s) => (s.col !== col ? { col, desc: false } : !s.desc ? { col, desc: true } : { col: null, desc: false }));
  const sql = table ? `SELECT * FROM records."${line.schema}"."${table}"` : null;
  const tab = (active: boolean): React.CSSProperties => ({
    ...chip, fontSize: 12, padding: '5px 11px', color: active ? '#f4f4f5' : '#a1a1aa',
    borderColor: active ? THEME_BRAND : '#3f3f46',
  });
  return (
    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderBottom: '1px solid #27272a', flexWrap: 'wrap' }}>
        <button onClick={onToggleLeft} title={leftCollapsed ? 'Show ponds' : 'Hide ponds'} style={chip}>
          {leftCollapsed ? '›' : '‹'}
        </button>
        <span style={{ color: '#f4f4f5', fontSize: 13.5, fontWeight: 700 }}>
          {line.name} <span style={{ color: '#71717a', fontWeight: 400 }}>v{line.major}</span>
        </span>
        {line.state !== 'live' && (
          <span title={line.state === 'retired' ? 'A newer major took over: this data is read-only' : 'Not yet promoted'}
                style={{ fontSize: 11, color: '#a1a1aa', border: '1px solid #3f3f46', borderRadius: 4, padding: '1px 6px' }}>
            {line.state}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {line.records && (
          <button style={tab(sel.view === 'records')} onClick={() => onSelect({ ...sel, view: 'records' })}>Records</button>
        )}
        {line.media && (
          <button style={tab(sel.view === 'media')} onClick={() => onSelect({ ...sel, view: 'media' })}>Media</button>
        )}
        <button onClick={onClose} style={chip}>✕</button>
      </div>
      {sel.view === 'records' && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px', borderBottom: '1px solid #27272a', fontSize: 12, color: '#a1a1aa' }}>
            <select value={table ?? ''} onChange={(e) => onSelect({ ...sel, view: 'records', table: e.target.value })}
                    style={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 6, padding: '4px 8px',
                             color: '#e4e4e7', fontSize: 12, fontFamily: 'inherit', outline: 'none' }}>
              {(line.tables ?? []).map((t) => <option key={t.table} value={t.table}>{t.table}</option>)}
            </select>
            {table && <span style={{ color: '#71717a' }}>records.{line.schema}.{table}</span>}
          </div>
          <div style={{ flex: 1, minHeight: 0 }}>
            {sql ? (
              <VirtualGrid key={`${sql}:${sort.col}:${sort.desc}:${runSeq}`}
                           query={{ pond: `${line.name}@${line.major}`, sql, orderBy: sort.col, orderDesc: sort.desc }}
                           onTotal={setTotal} onRowClick={() => undefined} sort={sort} onSort={cycleSort}
                           onRetry={() => setRunSeq((n) => n + 1)} />
            ) : (
              <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>No Records tables yet.</div>
            )}
          </div>
        </>
      )}
      {sel.view === 'media' && (
        <div style={{ flex: 1, minHeight: 0 }}>
          <MediaPanel line={line} canManage={canManage && line.state !== 'retired'} onChanged={onChanged} />
        </div>
      )}
    </div>
  );
}

function MediaPanel({ line, canManage, onChanged }: { line: AppDataLine; canManage: boolean; onChanged: () => void }) {
  const [files, setFiles] = useState<MediaFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [folder, setFolder] = useState('uploads');
  const [confirm, setConfirm] = useState<ConfirmOpts | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setFiles(await fetchMedia(line.name, line.major));
    } catch (e) {
      on401(e);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [line.name, line.major]);
  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  const act = async (key: string, run: () => Promise<void>) => {
    setBusy(key);
    setError(null);
    try {
      await run();
    } catch (e) {
      on401(e);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  const upload = (list: FileList | null) => {
    if (!list || list.length === 0) return;
    const dir = folder.trim().replace(/^\/+|\/+$/g, '');
    void act('upload', async () => {
      for (const f of Array.from(list)) await uploadMedia(line.name, line.major, dir ? `${dir}/${f.name}` : f.name, f);
      await load();
      onChanged();
    });
  };
  const remove = (f: MediaFile) => setConfirm({
    title: `Delete “${f.path}”?`,
    body: 'The file is removed from the App’s Media. The App and the Ponds reading it no longer see it.',
    confirmLabel: 'Delete file',
    action: () => act(f.path, async () => { await deleteMedia(line.name, line.major, f.path); await load(); onChanged(); }),
  });

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {canManage && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px', borderBottom: '1px solid #27272a', fontSize: 12, color: '#a1a1aa' }}>
          <span>Upload into</span>
          <input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="folder"
                 style={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 6, color: '#e4e4e7',
                          padding: '4px 8px', fontSize: 12, fontFamily: 'inherit', outline: 'none', width: 160 }} />
          <button onClick={() => input.current?.click()} disabled={busy === 'upload'}
                  style={{ ...chip, fontSize: 12, color: THEME_BRAND, borderColor: `${THEME_BRAND}66` }}>
            {busy === 'upload' ? 'Uploading…' : 'Choose files'}
          </button>
          <input ref={input} type="file" multiple style={{ display: 'none' }}
                 onChange={(e) => { upload(e.target.files); e.target.value = ''; }} />
        </div>
      )}
      {error && <div style={{ padding: '8px 14px', color: '#ef4444', fontSize: 12.5 }}>{error}</div>}
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {files == null ? (
          <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>
        ) : files.length === 0 ? (
          <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>No files.</div>
        ) : (
          <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 12, color: '#d4d4d8' }}>
            <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
              <tr>
                <th style={th({})}>path</th>
                <th style={th({ textAlign: 'right' })}>size</th>
                <th style={th({})}>modified</th>
                <th style={th({ textAlign: 'right' })}></th>
              </tr>
            </thead>
            <tbody>
              {files.map((f, i) => (
                <tr key={f.path} style={{ height: ROW_H, background: i % 2 ? '#121217' : 'transparent' }}>
                  <td style={td({ color: '#e4e4e7' })} title={f.path}>{f.path}</td>
                  <td style={td({ textAlign: 'right', color: '#a1a1aa' })}>{fmtBytes(f.size)}</td>
                  <td style={td({ color: '#a1a1aa' })}>{f.modified ? fmtTs(f.modified) : ''}</td>
                  <td style={td({ textAlign: 'right' })}>
                    <span style={{ display: 'inline-flex', gap: 6, justifyContent: 'flex-end' }}>
                      <button onClick={() => act(f.path, () => downloadMedia(line.name, line.major, f.path))}
                              disabled={busy === f.path}
                              style={{ background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5, padding: '2px 10px',
                                       color: busy === f.path ? '#52525b' : THEME_BRAND, fontSize: 11.5, fontWeight: 700,
                                       cursor: 'pointer', fontFamily: 'inherit' }}>
                        {busy === f.path ? '…' : 'Download'}
                      </button>
                      {canManage && (
                        <button onClick={() => remove(f)} disabled={busy === f.path}
                                style={{ background: 'transparent', border: `1px solid ${THEME_BLOCKED}66`, borderRadius: 5,
                                         padding: '2px 10px', color: THEME_BLOCKED, fontSize: 11.5, fontWeight: 700,
                                         cursor: 'pointer', fontFamily: 'inherit' }}>
                          Delete
                        </button>
                      )}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {confirm && <ConfirmDialog opts={confirm} onClose={() => setConfirm(null)} />}
    </div>
  );
}

function TableRow({
  pond, major, t, canManage, onPick, onExpose,
}: {
  pond: string; major: number; t: CatalogTable; canManage: boolean; onPick: () => void; onExpose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard?.writeText(copyName(pond, major, t.table)).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1100);
    }).catch(() => undefined);
  };
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 10px 3px 22px' }}>
      {canManage && (
        <span role="button" title={t.exposed ? 'Serviceable — click to hide' : 'Hidden — click to expose'}
              onClick={(e) => { e.stopPropagation(); onExpose(); }}
              style={{ cursor: 'pointer', color: t.exposed ? THEME_BRAND : '#52525b', fontSize: 12 }}>
          {t.exposed ? '●' : '○'}
        </span>
      )}
      <span onClick={onPick} title="Open in the viewer"
            style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                     cursor: 'pointer', fontSize: 12, color: t.exposed ? '#e4e4e7' : '#71717a' }}>
        {t.table}
      </span>
      <button onClick={copy} title={`Copy ${copyName(pond, major, t.table)}`}
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: '0 2px',
                       color: copied ? THEME_SUCCESS : '#71717a', fontSize: 11, fontFamily: 'inherit' }}>
        {copied ? '✓' : '⧉'}
      </button>
    </div>
  );
}

function ConnectFooter({ catchment, connect }: { catchment: string | null; connect: Record<string, string> }) {
  const wires = Object.entries(connect);
  return (
    <div style={{ borderTop: '1px solid #27272a', padding: '7px 10px', fontSize: 10, color: '#71717a', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {catchment && <span style={{ color: '#a1a1aa' }}>{catchment}</span>}
      {wires.length === 0
        ? <span style={{ color: '#52525b' }}>no wire ports</span>
        : wires.map(([k, v]) => (
            <span key={k} title="Point a BI tool / client here (password = your API key)">
              {k}: <span style={{ color: '#a1a1aa' }}>{v}</span>
            </span>
          ))}
    </div>
  );
}

// ─── The Data Viewer (right panel) ───────────────────────────────────────────

function DataViewer({
  pondName, major, servedMajor, canManage, initialTable, selectSig, leftCollapsed, onToggleLeft, onPromote, onClose,
}: {
  pondName: string;
  major: number;
  servedMajor: number | null;
  canManage: boolean;
  initialTable?: string;
  selectSig: Pending | null;
  leftCollapsed: boolean;
  onToggleLeft: () => void;
  onPromote: () => void;
  onClose: () => void;
}) {
  const pondId = `${pondName}@${major}`;
  const hasObjects = useLiveStore((s) => s.pondInfo[pondId]?.hasObjects ?? false);

  // Tabular data vs non-tabular Objects (models/blobs). Objects are a separate published surface.
  const [view, setView] = useState<'tables' | 'objects'>('tables');
  const [tables, setTables] = useState<TableInfo[] | null>(null);
  const [table, setTable] = useState<string | null>(null);
  const [mode, setMode] = useState<'browse' | 'query'>('browse');
  const [sqlText, setSqlText] = useState('');
  const [activeSql, setActiveSql] = useState('');
  // On a phone, either way up, the freshness window and the SQL box fold behind one bar (rarely used, and they took
  // most of the screen).
  const isMobile = useIsMobile();
  const isShort = useIsShort();
  const foldTools = isMobile || isShort;
  const [toolsOpen, setToolsOpen] = useState(false);
  const [runSeq, setRunSeq] = useState(0); // bumps on Run, so the same SQL runs again (after a cancel, say)
  const [expanded, setExpanded] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [tablesError, setTablesError] = useState<string | null>(null);
  // Trickle freshness window (browse only): inclusive [fLo, fHi]; null = unbounded.
  const [freshness, setFreshness] = useState<string[]>([]);
  const [floor, setFloor] = useState<string | null>(null);
  const [fLo, setFLo] = useState<string | null>(null);
  const [fHi, setFHi] = useState<string | null>(null);
  // The record whose changelog history is open (merge only).
  const [historyPk, setHistoryPk] = useState<Record<string, unknown> | null>(null);
  // A pending destructive confirmation (themed in-app dialog), shared by the table + object deletes.
  const [confirm, setConfirm] = useState<ConfirmOpts | null>(null);
  // Opt-in column sort (null = the efficient base order). Clicking a header cycles asc → desc → off.
  const [sort, setSort] = useState<{ col: string | null; desc: boolean }>({ col: null, desc: false });
  const cycleSort = (col: string) => {
    const next = sort.col !== col ? { col, desc: false } : !sort.desc ? { col, desc: true } : { col: null, desc: false };
    // A sort reads the whole table: past SLOW_SORT_ROWS, ask first. Turning a sort off never does.
    if (next.col && total != null && total > SLOW_SORT_ROWS) {
      setConfirm({
        title: `Sort ${total.toLocaleString()} rows?`,
        body: `Sorting by ${next.col} reads the whole table, so each page can take a while.`,
        confirmLabel: 'Sort',
        tone: 'neutral',
        action: () => setSort(next),
      });
      return;
    }
    setSort(next);
  };
  const taRef = useRef<HTMLTextAreaElement>(null);
  // The selectSig nonce already consumed (the mount picks `initialTable`; later tree clicks fire here).
  const lastSig = useRef<number | undefined>(selectSig?.nonce);

  const tableInfo = tables?.find((t) => t.name === table) ?? null;
  const trickle: TrickleMode | null = tableInfo?.trickle ?? null;

  const loadFreshness = useCallback(
    async (tbl: string, ti: TableInfo | undefined) => {
      if (!ti?.trickle) {
        setFreshness([]);
        setFloor(null);
        return;
      }
      try {
        const r = await fetchFreshness(pondId, tbl);
        setFreshness(r.freshness);
        setFloor(r.floor);
      } catch (e) {
        on401(e);
        setFreshness([]);
      }
    },
    [pondId]
  );

  const openTable = useCallback((t: string, ti: TableInfo | undefined) => {
    setTable(t);
    setMode('browse');
    setSqlText(browseSql(pondName, major, servedMajor, t));
    setFLo(null);
    setFHi(null);
    setTotal(null);
    setSort({ col: null, desc: false }); // columns differ between tables
    void loadFreshness(t, ti);
  }, [pondName, major, servedMajor, loadFreshness]);

  // Load the table list once on mount; select `initialTable` (a tree click) else the first table.
  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        const ts = await fetchTables(pondId);
        setTables(ts);
        const want = (initialTable && ts.find((x) => x.name === initialTable)) ? initialTable : ts[0]?.name;
        if (want) {
          openTable(want, ts.find((x) => x.name === want));
        } else if (hasObjects) {
          setView('objects'); // no tables — open straight to the Objects tab
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

  // A tree table-click within the CURRENT pond+major (no remount) selects the table in place.
  useEffect(() => {
    if (!selectSig || !tables) return;
    if (lastSig.current === selectSig.nonce) return;
    const ti = tables.find((x) => x.name === selectSig.table);
    if (!ti) return;
    lastSig.current = selectSig.nonce;
    const t = setTimeout(() => { setView('tables'); openTable(selectSig.table, ti); }, 0);
    return () => clearTimeout(t);
  }, [selectSig, tables, openTable]);

  useEffect(() => {
    // When a confirm dialog is open it owns Escape (it closes itself); don't also close the modal.
    const onKey = (e: KeyboardEvent) =>
      e.key === 'Escape' && !confirm && (historyPk ? setHistoryPk(null) : onClose());
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, historyPk, confirm]);

  const expand = () => {
    setExpanded(true);
    const ta = taRef.current;
    if (ta && ta.offsetHeight < 110) ta.style.height = '110px';
  };
  const collapse = () => {
    setExpanded(false);
    if (taRef.current) taRef.current.style.height = '38px';
  };

  const selectTable = (t: string) => openTable(t, tables?.find((x) => x.name === t));
  const deleteCurrentTable = () => {
    if (!table) return;
    const target = table;
    // A Trickle companion (changelog/band/droplog/base) is one deletable unit with its base table.
    const base = baseTableName(target);
    const baseMode = tables?.find((t) => t.name === base)?.trickle ?? null;
    const notes: string[] = [];
    if (base !== target) notes.push(`“${target}” is part of table “${base}”, so the whole table is deleted.`);
    if (baseMode === 'merge') notes.push('Its changelog is removed too.');
    else if (baseMode === 'append') notes.push('It is an append Trickle — its droplog and accumulated history are dropped.');
    setConfirm({
      title: `Delete “${base}”?`,
      body: `This cannot be undone.${notes.length ? ' ' + notes.join(' ') : ''}`,
      confirmLabel: 'Delete table',
      action: async () => {
        try {
          await deleteTable(pondId, target); // the Catchment resolves a companion to its base
          const ts = await fetchTables(pondId);
          setTables(ts);
          if (ts[0]) openTable(ts[0].name, ts[0]);
          else setTable(null);
        } catch (e) {
          on401(e);
          setTablesError(e instanceof Error ? e.message : String(e));
        }
      },
    });
  };
  const runQuery = () => {
    if (!sqlText.trim()) return;
    setActiveSql(sqlText);
    setRunSeq((n) => n + 1);
    setMode('query');
    setTotal(null);
    setSort({ col: null, desc: false });
  };
  const clearQuery = () => {
    setMode('browse');
    setTotal(null);
    setSort({ col: null, desc: false });
    if (table) setSqlText(browseSql(pondName, major, servedMajor, table));
  };

  // The active grid query + a key that remounts the grid whenever the source/window/sort changes. A
  // hand-written query runs cross-pond through the serving core; a browse stays on this pond+major.
  let query: DataQuery | null =
    mode === 'query'
      ? { pond: pondId, sql: activeSql }
      : table
        ? trickle
          ? { pond: pondId, table, trickle, pk: tableInfo?.pk ?? [], fLo, fHi }
          : { pond: pondId, table }
        : null;
  if (query && sort.col) query = { ...query, orderBy: sort.col, orderDesc: sort.desc };
  const queryKey =
    (mode === 'query' ? `sql:${activeSql}` : `tbl:${table}:${trickle}:${fLo}:${fHi}`) + `:${sort.col}:${sort.desc}:${runSeq}`;

  const canPromote = canManage && servedMajor != null && major !== servedMajor;

  return (
    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', position: 'relative' }}>
      {/* Top row: [◂ tree] pond · Promote | table select | Delete | rows | ✕. It wraps on a narrow screen, with ✕
          pinned top-right so the viewer can always be closed. */}
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, padding: '10px 52px 10px 14px', borderBottom: '1px solid #27272a', flexShrink: 0, position: 'relative' }}>
        <button onClick={onToggleLeft} title={leftCollapsed ? 'Show ponds' : 'Hide ponds'} style={chip}>
          {leftCollapsed ? '›' : '‹'}
        </button>
        <span style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7' }}>{pondName}</span>
        <span title={major === servedMajor ? 'the served (default) major' : undefined}
              style={{ fontSize: 11, color: major === servedMajor ? THEME_SUCCESS : '#71717a' }}>
          v{major}
        </span>
        {canPromote && (
          <button
            onClick={onPromote}
            title={`Make v${major} the served (default) major`}
            style={{ ...chip, color: THEME_SUCCESS, borderColor: THEME_SUCCESS }}
          >
            Promote
          </button>
        )}
        {hasObjects && tables && tables.length > 0 && (
          <span style={{ display: 'inline-flex', border: '1px solid #3f3f46', borderRadius: 6, overflow: 'hidden' }}>
            {(['tables', 'objects'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                style={{
                  background: view === v ? '#27272a' : 'transparent', border: 'none', padding: '5px 11px',
                  color: view === v ? '#e4e4e7' : '#71717a', fontSize: 12, fontWeight: 700, cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                {v === 'tables' ? 'Tables' : 'Objects'}
              </button>
            ))}
          </span>
        )}
        {view === 'tables' && tables && tables.length > 0 && (
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
                <option key={t.name} value={t.name}>{t.name}{t.trickle ? ` · ${t.trickle}` : ''}</option>
              ))}
            </select>
            <span style={{ position: 'absolute', right: 9, pointerEvents: 'none', color: '#71717a', fontSize: 9 }}>▼</span>
          </span>
        )}
        {view === 'tables' && mode === 'browse' && table && canManage && (
          <button
            onClick={deleteCurrentTable}
            title={`Delete "${table}" (drops its data + state, then rebuilds)`}
            style={{
              background: 'transparent', border: `1px solid ${THEME_BLOCKED}66`, borderRadius: 5,
              color: THEME_BLOCKED, fontSize: 11.5, fontWeight: 700, padding: '4px 9px', cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            Delete
          </button>
        )}
        {view === 'tables' && mode === 'query' && (
          <span style={{ fontSize: 10, fontWeight: 700, color: '#ee9333', letterSpacing: '0.06em' }}>QUERY</span>
        )}
        {view === 'tables' && (
          <span style={{ fontSize: 11, color: '#71717a' }}>
            {total == null ? '' : `${total.toLocaleString()} row${total === 1 ? '' : 's'}`}
          </span>
        )}
        <button onClick={onClose} title="Close (Esc)" style={{ ...chip, position: 'absolute', top: 10, right: 14 }}>✕</button>
      </div>

      {foldTools && view === 'tables' && (
        <div
          onClick={() => setToolsOpen((o) => !o)}
          style={{ padding: '8px 14px', borderBottom: '1px solid #27272a', cursor: 'pointer', userSelect: 'none',
            fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.06em', flexShrink: 0 }}
        >
          {toolsOpen ? '▾' : '▸'} {mode === 'browse' && trickle ? 'FRESHNESS & SQL' : 'SQL'}
          {!toolsOpen && (mode === 'query' || fLo || fHi) && (
            <span style={{ color: '#ee9333', fontWeight: 400, marginLeft: 8, letterSpacing: 0 }}>
              {mode === 'query' ? 'custom query' : 'window set'}
            </span>
          )}
        </div>
      )}

      {/* Freshness window — only for a Trickle table being browsed */}
      {(!foldTools || toolsOpen) && view === 'tables' && mode === 'browse' && trickle && (
        <FreshnessWindow freshness={freshness} floor={floor} fLo={fLo} fHi={fHi} setLo={setFLo} setHi={setFHi} />
      )}

      {/* SQL box — hand-written SQL runs cross-pond (serving core); browse names are pre-filled per major */}
      {(!foldTools || toolsOpen) && view === 'tables' && (
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
            placeholder='SELECT * FROM "pond_v1"."table" — or JOIN across ponds in this Catchment'
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
      )}

      {/* Body: the tabular grid, or the Objects list */}
      <div style={{ flex: 1, minHeight: 0 }}>
        {view === 'objects' ? (
          <ObjectsPanel pondId={pondId} canManage={canManage} requestConfirm={setConfirm} />
        ) : tablesError ? (
          <div style={{ padding: 16, color: '#ef4444', fontSize: 12.5 }}>{tablesError}</div>
        ) : tables == null ? (
          <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>
        ) : query == null ? (
          <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>
            This Pond has no {canManage ? 'exported' : 'serviceable'} tables.{hasObjects ? ' See the Objects tab.' : ''}
          </div>
        ) : (
          <VirtualGrid key={queryKey} query={query} onTotal={setTotal} onRowClick={setHistoryPk} sort={sort} onSort={cycleSort}
                       onRetry={() => setRunSeq((n) => n + 1)} />
        )}
      </div>

      {historyPk && table && (
        <HistoryOverlay pond={pondId} pondName={pondName} table={table} pk={historyPk} onClose={() => setHistoryPk(null)} />
      )}

      {confirm && <ConfirmDialog opts={confirm} onClose={() => setConfirm(null)} />}
    </div>
  );
}

// ─── Objects list ────────────────────────────────────────────────────────────

function fmtBytes(n: number | null): string {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

function ObjectsPanel({ pondId, canManage, requestConfirm }: {
  pondId: string; canManage: boolean; requestConfirm: (o: ConfirmOpts) => void;
}) {
  const [objects, setObjects] = useState<ObjectInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setObjects(await fetchObjects(pondId));
    } catch (e) {
      on401(e);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pondId]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  const download = async (o: ObjectInfo) => {
    setBusy(o.name);
    try {
      await downloadObject(pondId, o);
    } catch (e) {
      on401(e);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const remove = (o: ObjectInfo) => {
    requestConfirm({
      title: `Delete “${o.name}”?`,
      body: 'This Object is removed. It returns only if a Ripple writes it again.',
      confirmLabel: 'Delete object',
      action: async () => {
        setBusy(o.name);
        try {
          await deleteObject(pondId, o.name);
          await load();
        } catch (e) {
          on401(e);
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setBusy(null);
        }
      },
    });
  };

  if (error) return <div style={{ padding: 16, color: '#ef4444', fontSize: 12.5 }}>{error}</div>;
  if (objects == null) return <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>;
  if (objects.length === 0) return <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>No objects.</div>;

  return (
    <div style={{ height: '100%', overflow: 'auto' }}>
      <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 12, color: '#d4d4d8' }}>
        <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
          <tr>
            <th style={th({})}>name</th>
            <th style={th({})}>kind</th>
            <th style={th({ textAlign: 'right' })}>size</th>
            <th style={th({})}>freshness</th>
            <th style={th({ textAlign: 'right' })}></th>
          </tr>
        </thead>
        <tbody>
          {objects.map((o, i) => (
            <tr key={o.name} style={{ height: ROW_H, background: i % 2 ? '#121217' : 'transparent' }}>
              <td style={td({ color: '#e4e4e7', fontWeight: 600 })} title={o.name}>{o.name}</td>
              <td style={td({ color: '#a1a1aa' })}>{o.is_dir ? 'directory' : `file${o.ext ? ` · ${o.ext}` : ''}`}</td>
              <td style={td({ textAlign: 'right', color: '#a1a1aa' })}>{fmtBytes(o.size)}</td>
              <td style={td({ color: '#a1a1aa' })}>{o.f ? fmtTs(o.f) : <span style={{ color: '#3f3f46' }}>·</span>}</td>
              <td style={td({ textAlign: 'right' })}>
                <span style={{ display: 'inline-flex', gap: 6, justifyContent: 'flex-end' }}>
                  <button
                    onClick={() => download(o)}
                    disabled={busy === o.name}
                    title={o.is_dir ? 'Download as a zip' : 'Download'}
                    style={{
                      background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5, padding: '2px 10px',
                      color: busy === o.name ? '#52525b' : '#06c4e6', fontSize: 11.5, fontWeight: 700,
                      cursor: busy === o.name ? 'default' : 'pointer', fontFamily: 'inherit',
                    }}
                  >
                    {busy === o.name ? '…' : o.is_dir ? 'Download .zip' : 'Download'}
                  </button>
                  {canManage && (
                    <button
                      onClick={() => remove(o)}
                      disabled={busy === o.name}
                      title="Delete this Object"
                      style={{
                        background: 'transparent', border: `1px solid ${THEME_BLOCKED}66`, borderRadius: 5,
                        padding: '2px 10px', color: THEME_BLOCKED, fontSize: 11.5, fontWeight: 700,
                        cursor: busy === o.name ? 'default' : 'pointer', fontFamily: 'inherit',
                      }}
                    >
                      Delete
                    </button>
                  )}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Freshness window control ────────────────────────────────────────────────

// One bound: a run-freshness select (newest-first) with the given specials, plus a datetime override.
function Bound({
  value, onChange, freshness, specials, lo,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
  freshness: string[];
  specials: { v: string; label: string }[];
  lo: string | null; // the lower bound (for "= from")
}) {
  const toLocal = (iso: string | null) => {
    if (!iso) return '';
    const d = new Date(iso);
    const p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const selVal = value && freshness.includes(value) ? value : '';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <select
        value={selVal}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v === '__same__' ? lo : v === '' ? null : v);
        }}
        style={{
          appearance: 'none', WebkitAppearance: 'none', MozAppearance: 'none',
          background: '#18181b', border: '1px solid #3f3f46', borderRadius: 6, padding: '4px 22px 4px 8px',
          color: '#e4e4e7', fontSize: 12, fontFamily: 'inherit', outline: 'none', cursor: 'pointer',
        }}
      >
        {specials.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        {freshness.map((f) => <option key={f} value={f}>{fmtTs(f)}</option>)}
      </select>
      <input
        type="datetime-local"
        value={toLocal(selVal ? null : value)}
        onChange={(e) => onChange(e.target.value ? new Date(e.target.value).toISOString() : null)}
        title="or pick a time"
        style={{
          background: '#18181b', border: '1px solid #3f3f46', borderRadius: 6, padding: '3px 6px',
          color: '#a1a1aa', fontSize: 11, fontFamily: 'inherit', outline: 'none', colorScheme: 'dark',
        }}
      />
    </span>
  );
}

function FreshnessWindow({
  freshness, floor, fLo, fHi, setLo, setHi,
}: {
  freshness: string[];
  floor: string | null;
  fLo: string | null;
  fHi: string | null;
  setLo: (v: string | null) => void;
  setHi: (v: string | null) => void;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10, padding: '8px 14px', borderBottom: '1px solid #27272a', flexShrink: 0, fontSize: 12, color: '#a1a1aa' }}>
      <span style={{ fontWeight: 700, color: '#71717a' }}>Freshness</span>
      <Bound value={fLo} onChange={setLo} freshness={freshness} lo={fLo} specials={[{ v: '', label: 'Earliest' }]} />
      <span style={{ color: '#52525b' }}>→</span>
      <Bound value={fHi} onChange={setHi} freshness={freshness} lo={fLo} specials={[{ v: '', label: 'Now' }, { v: '__same__', label: '= from' }]} />
      {floor && <span style={{ color: '#52525b', fontSize: 11 }}>floor {fmtTs(floor)}</span>}
    </div>
  );
}

// ─── Virtual grid ──────────────────────────────────────────────────────────────

const SLOW_MS = 1000; // a request in flight this long offers Cancel

function VirtualGrid({
  query, onTotal, onRowClick, sort, onSort, onRetry,
}: {
  query: DataQuery;
  onTotal: (n: number) => void;
  onRowClick: (pk: Record<string, unknown>) => void;
  sort: { col: string | null; desc: boolean };
  onSort: (col: string) => void;
  onRetry: () => void;
}) {
  const [total, setTotal] = useState<number | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [windowStart, setWindowStart] = useState(0);
  const [rows, setRows] = useState<unknown[][]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inFlight = useRef(false);
  const queued = useRef<number | null>(null);
  const loaded = useRef({ start: 0, end: 0 });
  const fetchWindowRef = useRef<(start: number) => void>(() => {});
  // Requests in flight (id → abort), so a slow one can be cancelled here and on the Catchment.
  const pending = useRef(new Map<string, AbortController>());
  const [slow, setSlow] = useState(false);
  const [cancelled, setCancelled] = useState(false);
  const cancelledRef = useRef(false);
  const tracked = useCallback(<T,>(run: (c: { queryId: string; signal: AbortSignal }) => Promise<T>): Promise<T> => {
    // Not crypto.randomUUID: it exists only in a secure context, and a phone on the LAN reaches the UI over http.
    const queryId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
    const ctl = new AbortController();
    pending.current.set(queryId, ctl);
    const timer = setTimeout(() => setSlow(pending.current.size > 0), SLOW_MS);
    return run({ queryId, signal: ctl.signal }).finally(() => {
      clearTimeout(timer);
      pending.current.delete(queryId);
      if (pending.current.size === 0) setSlow(false);
    });
  }, []);
  const cancel = () => {
    cancelledRef.current = true;
    setCancelled(true);
    for (const [id, ctl] of pending.current) {
      void cancelQuery(id).catch(() => {});
      ctl.abort();
    }
  };

  const fetchWindow = useCallback(
    async (start: number) => {
      // Serialise: one /page request in flight at a time. While one runs, keep only the latest window
      // the user has scrolled to — a fast scroll (or scrollbar drag) collapses to the in-flight request
      // plus a single follow-up, instead of one request per row crossed (which hung the page).
      if (inFlight.current) {
        queued.current = start;
        return;
      }
      inFlight.current = true;
      try {
        const res = await tracked((c) => fetchPage({ ...query, limit: CHUNK, offset: start }, c));
        setColumns((prev) => (res.columns.length ? res.columns : prev));
        setRows(res.rows);
        setWindowStart(start);
        loaded.current = { start, end: start + res.rows.length };
      } catch (e) {
        on401(e);
        if (!cancelledRef.current) setError(e instanceof Error ? e.message : String(e));
      } finally {
        inFlight.current = false;
        // Land the most recent window scrolled to while this request was in flight.
        const next = queued.current;
        queued.current = null;
        if (next !== null && next !== loaded.current.start) void fetchWindowRef.current(next);
      }
    },
    [query, tracked]
  );
  useEffect(() => {
    fetchWindowRef.current = fetchWindow;
  }, [fetchWindow]);

  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        // Always fetch the first window — the count only *sizes* the scroll; gating the page on it would
        // let a 0/stale count silently hide real rows (and suppress the /page request entirely).
        const [c] = await Promise.all([tracked((cc) => fetchCount(query, cc)), fetchWindow(0)]);
        setTotal(c);
        onTotal(c);
      } catch (e) {
        on401(e);
        if (!cancelledRef.current) setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    }, 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Trust the loaded rows over the reported count for sizing/empty checks — never let a bad count hide
  // real rows. `effTotal` is at least what we've actually loaded.
  const effTotal = Math.max(total ?? 0, windowStart + rows.length);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const firstVisible = Math.floor(el.scrollTop / ROW_H);
    const lastVisible = firstVisible + Math.ceil(el.clientHeight / ROW_H);
    const { start, end } = loaded.current;
    const needAbove = start > 0 && firstVisible < start + OVERSCAN;
    const needBelow = end < effTotal && lastVisible > end - OVERSCAN;
    if (needAbove || needBelow) {
      const newStart = Math.max(0, Math.min(firstVisible - OVERSCAN, Math.max(0, effTotal - CHUNK)));
      if (newStart !== start) void fetchWindow(newStart);
    }
  };

  const button = { background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5, color: '#e4e4e7',
    fontSize: 11.5, fontWeight: 700, padding: '3px 10px', cursor: 'pointer', fontFamily: 'inherit' } as const;
  if (cancelled) {
    return (
      <div style={{ padding: 16, color: '#a1a1aa', fontSize: 12.5, display: 'flex', alignItems: 'center', gap: 12 }}>
        Query cancelled. <button onClick={onRetry} style={button}>Run again</button>
      </div>
    );
  }
  const running = slow && (
    <div style={{ position: 'sticky', top: 0, zIndex: 2, display: 'flex', alignItems: 'center', gap: 12, padding: '6px 14px',
      background: '#18181b', borderBottom: '1px solid #27272a', color: '#a1a1aa', fontSize: 12 }}>
      Running… <button onClick={cancel} style={button}>Cancel</button>
    </div>
  );
  if (error) return <div style={{ padding: 16, color: '#ef4444', fontSize: 12.5, whiteSpace: 'pre-wrap' }}>{error}</div>;
  if (loading) return running || <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>;
  if (rows.length === 0 || columns.length === 0) return <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>No rows.</div>;

  // Hide the active-flag column; it drives row colour instead. Merge rows are clickable (→ history).
  // On a raw __changelog view the Z-set weight column is kept visible (relabelled Δ) but also tints
  // retractions (d < 0) in the BLOCKED theme.
  const activeIdx = columns.indexOf(ACTIVE);
  const deltaIdx = columns.indexOf(DELTA);
  const display = columns.map((_, i) => i).filter((i) => i !== activeIdx);
  const clickable = query.trickle === 'merge';
  const pkCols = query.pk ?? [];

  const topPad = windowStart * ROW_H;
  const botPad = Math.max(0, (effTotal - windowStart - rows.length) * ROW_H);
  const tableWidth = NUM_W + display.length * COL_W;

  return (
    <div ref={scrollRef} onScroll={onScroll} style={{ height: '100%', overflow: 'auto' }}>
      {running}
      <table style={{ tableLayout: 'fixed', borderCollapse: 'collapse', width: tableWidth, fontSize: 12, color: '#d4d4d8' }}>
        <colgroup>
          <col style={{ width: NUM_W }} />
          {display.map((i) => <col key={i} style={{ width: COL_W }} />)}
        </colgroup>
        <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
          <tr>
            <th style={th({ color: '#52525b', textAlign: 'right' })}>#</th>
            {display.map((i) => {
              const c = columns[i];
              const arrow = sort.col === c ? (sort.desc ? ' ▼' : ' ▲') : '';
              return (
                <th
                  key={i}
                  onClick={() => onSort(c)}
                  title={`${c} — click to sort`}
                  style={th({ cursor: 'pointer', userSelect: 'none', color: sort.col === c ? '#e4e4e7' : undefined })}
                >
                  {(COL_LABELS[c] ?? c) + arrow}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {topPad > 0 && (
            <tr style={{ height: topPad }}><td colSpan={display.length + 1} style={{ padding: 0, border: 0 }} /></tr>
          )}
          {rows.map((row, i) => {
            const idx = windowStart + i;
            const active = activeIdx >= 0 ? Number(row[activeIdx]) : 1;
            const inactive = active < 0 || (deltaIdx >= 0 && Number(row[deltaIdx]) < 0);
            const replaced = active === 0;
            return (
              <tr
                key={idx}
                onClick={clickable ? () => onRowClick(Object.fromEntries(pkCols.map((c) => [c, row[columns.indexOf(c)]]))) : undefined}
                style={{
                  height: ROW_H,
                  cursor: clickable ? 'pointer' : 'default',
                  background: inactive ? `${THEME_BLOCKED}22` : idx % 2 ? '#121217' : 'transparent',
                  color: inactive ? '#fca5a5' : replaced ? '#71717a' : undefined,
                }}
              >
                <td style={td({ color: '#52525b', textAlign: 'right' })}
                  title={active < 0 ? 'Deleted' : replaced ? 'Replaced by a later write' : undefined}>{idx + 1}</td>
                {display.map((ci) => (
                  <td key={ci} style={td({})} title={row[ci] == null ? '' : String(row[ci])}>
                    {row[ci] == null
                      ? <span style={{ color: '#3f3f46' }}>·</span>
                      : columns[ci] === FRESH ? fmtTs(String(row[ci])) : String(row[ci])}
                  </td>
                ))}
              </tr>
            );
          })}
          {botPad > 0 && (
            <tr style={{ height: botPad }}><td colSpan={display.length + 1} style={{ padding: 0, border: 0 }} /></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ─── Per-record history (merge) ──────────────────────────────────────────────

function HistoryOverlay({
  pond, pondName, table, pk, onClose,
}: {
  pond: string;
  pondName: string;
  table: string;
  pk: Record<string, unknown>;
  onClose: () => void;
}) {
  const [data, setData] = useState<PageResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        setData(await fetchHistory(pond, table, pk));
      } catch (e) {
        on401(e);
        setError(e instanceof Error ? e.message : String(e));
      }
    }, 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const cols = data?.columns ?? [];
  const label = Object.entries(pk).map(([k, v]) => `${k}=${String(v)}`).join(', ');

  return (
    <div
      onClick={onClose}
      style={{
        position: 'absolute', inset: 0, zIndex: 10, display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(9, 9, 11, 0.6)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#101014', border: '1px solid #3f3f46', borderRadius: 10, width: '70%', maxHeight: '76%',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderBottom: '1px solid #27272a', flexShrink: 0 }}>
          <span style={{ fontSize: 12.5, fontWeight: 700, color: '#e4e4e7' }}>History</span>
          <span style={{ fontSize: 11.5, color: '#71717a' }}>{pondName}.{table} · {label}</span>
          <button
            onClick={onClose}
            style={{
              marginLeft: 'auto', background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5,
              color: '#a1a1aa', fontSize: 12, lineHeight: 1, padding: '3px 8px', cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            ✕
          </button>
        </div>
        <div style={{ overflow: 'auto', minHeight: 0 }}>
          {error ? (
            <div style={{ padding: 16, color: '#ef4444', fontSize: 12.5 }}>{error}</div>
          ) : data == null ? (
            <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>Loading…</div>
          ) : data.rows.length === 0 ? (
            <div style={{ padding: 16, color: '#71717a', fontSize: 12.5 }}>No changelog entries.</div>
          ) : (
            <table style={{ borderCollapse: 'collapse', fontSize: 12, color: '#d4d4d8', width: '100%' }}>
              <thead style={{ position: 'sticky', top: 0 }}>
                <tr>
                  {cols.map((c) => <th key={c} style={th({})} title={c}>{COL_LABELS[c] ?? c}</th>)}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row, i) => {
                  const ev = String(row[cols.indexOf(EVENT)] ?? '');
                  return (
                    <tr key={i} style={{ height: ROW_H, background: ev === 'delete' ? `${THEME_BLOCKED}22` : i % 2 ? '#121217' : 'transparent' }}>
                      {cols.map((c, j) => (
                        <td key={j} style={td({})} title={row[j] == null ? '' : String(row[j])}>
                          {row[j] == null ? <span style={{ color: '#3f3f46' }}>·</span>
                            : c === FRESH ? fmtTs(String(row[j]))
                            : c === EVENT ? <span style={{ color: EVENT_COLOR[ev], fontWeight: 700 }}>{ev}</span>
                            : String(row[j])}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
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
