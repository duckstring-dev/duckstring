'use client';

import { useEffect, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useLiveStore } from '@/lib/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';
import { RunHistory } from './RunHistory';
import { RunDetail } from './RunDetail';
import { DataViewerModal } from './DataViewerModal';

const POLL_MS = 1000;

// Shown when the Catchment answers 401 (it was started with --key / --generate-key). The key is
// kept in localStorage and sent as a Bearer header on every request; a wrong key re-raises 401 so
// the prompt simply stays up.
function KeyPrompt() {
  const submitApiKey = useLiveStore((s) => s.submitApiKey);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!value.trim() || busy) return;
    setBusy(true);
    try {
      await submitApiKey(value);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000, display: 'flex', alignItems: 'center',
      justifyContent: 'center', background: 'rgba(9, 9, 11, 0.85)', backdropFilter: 'blur(2px)',
    }}>
      <div style={{
        background: '#101014', border: '1px solid #27272a', borderRadius: 8, padding: '22px 26px',
        width: 360, maxWidth: '90vw', display: 'flex', flexDirection: 'column', gap: 12,
      }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7' }}>API key required</div>
        <div style={{ fontSize: 12, color: '#a1a1aa', lineHeight: 1.5 }}>
          This Catchment was started with an API key. Enter it to connect; it is stored in this
          browser only.
        </div>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }}
          placeholder="API key"
          style={{
            background: '#18181b', border: '1px solid #3f3f46', borderRadius: 5, padding: '8px 10px',
            color: '#e4e4e7', fontSize: 13, fontFamily: 'inherit', outline: 'none',
          }}
        />
        <button
          onClick={() => void submit()}
          disabled={!value.trim() || busy}
          style={{
            background: '#06c4e6', border: 'none', borderRadius: 5, padding: '8px 10px',
            color: '#09090b', fontSize: 13, fontWeight: 700, cursor: 'pointer',
            opacity: !value.trim() || busy ? 0.5 : 1, fontFamily: 'inherit',
          }}
        >
          {busy ? 'Connecting…' : 'Connect'}
        </button>
      </div>
    </div>
  );
}

// Mobile: the run panels collapse into a bottom sheet (RunHistory stacked over RunDetail)
// behind a header bar, so the canvas keeps the screen until runs are wanted.
function MobileRunsPanel() {
  const [open, setOpen] = useState(false);
  const selectedRun = useLiveStore((s) => s.selectedRun);
  return (
    <div style={{ borderTop: '1px solid #27272a', background: '#0c0c10', flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
      <div
        onClick={() => setOpen((v) => !v)}
        style={{ display: 'flex', alignItems: 'center', padding: '8px 14px', cursor: 'pointer', userSelect: 'none', flexShrink: 0 }}
      >
        <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
          {open ? '▾' : '▸'} RUNS
        </span>
      </div>
      {open && (
        // Fixed height so both sheets open can't push past a short phone's screen;
        // history and detail split it evenly (each scrolls internally).
        <div style={{ display: 'flex', flexDirection: 'column', height: '40dvh', minHeight: 0 }}>
          <div style={{ flex: 1, minHeight: 0, borderTop: '1px solid #27272a' }}>
            <RunHistory />
          </div>
          {selectedRun && (
            <div style={{ flex: 1, minHeight: 0, borderTop: '1px solid #27272a' }}>
              <RunDetail />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// The repair-mode toolbar — a top banner while the operator picks a connected set of Ponds on the
// canvas to force-rebuild now. The server validates connectivity on submit (its detail shows here).
function RepairBanner() {
  const repairMode = useLiveStore((s) => s.repairMode);
  const scope = useLiveStore((s) => s.repairScope);
  const error = useLiveStore((s) => s.repairError);
  const addDownstream = useLiveStore((s) => s.addRepairDownstream);
  const submit = useLiveStore((s) => s.submitRepair);
  const cancel = useLiveStore((s) => s.exitRepair);
  if (!repairMode) return null;

  const btn = (bg: string): React.CSSProperties => ({
    background: bg, border: 'none', borderRadius: 5, padding: '6px 12px', color: '#0a0a0a',
    fontSize: 12, fontWeight: 700, cursor: 'pointer', fontFamily: 'inherit',
  });
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '8px 14px',
      background: '#1a1206', borderBottom: '1px solid #ee9333', color: '#fbbf24', fontSize: 12,
    }}>
      <span style={{ fontWeight: 700 }}>Repair mode</span>
      <span style={{ color: '#a1a1aa' }}>
        Click Ponds to select a connected set to rebuild now · <b style={{ color: '#e4e4e7' }}>{scope.length}</b> selected
      </span>
      {error && <span style={{ color: '#ef4444' }}>{error}</span>}
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
        <button style={btn('#52525b')} onClick={() => addDownstream()} disabled={scope.length === 0}>Include downstream</button>
        <button style={btn('#a3e635')} onClick={() => void submit()} disabled={scope.length === 0}>Repair {scope.length}</button>
        <button style={btn('#71717a')} onClick={() => cancel()}>Cancel</button>
      </div>
    </div>
  );
}

export function App() {
  const refresh = useLiveStore((s) => s.refresh);
  const needsKey = useLiveStore((s) => s.needsKey);
  const isMobile = useIsMobile();

  useEffect(() => {
    // Long-poll loop: refresh() holds (via /api/status?since=) until the engine state changes, then
    // resolves at once, so the UI tracks state changes instantly rather than on a fixed timer. A short
    // gap between iterations (longer while disconnected) avoids a hot loop on instant returns/errors.
    let alive = true;
    (async () => {
      while (alive) {
        await refresh();
        if (!alive) break;
        await new Promise((r) => setTimeout(r, useLiveStore.getState().connected ? 100 : POLL_MS));
      }
    })();
    // A separate lightweight clock so freshness "age" keeps counting up between state changes.
    const clock = setInterval(() => useLiveStore.setState({ now: Date.now() }), POLL_MS);
    return () => {
      alive = false;
      clearInterval(clock);
    };
  }, [refresh]);

  return (
    <div className="ds-app" style={{ display: 'flex', flexDirection: 'column', width: '100%', overflow: 'hidden', fontFamily: 'ui-monospace, SFMono-Regular, monospace' }}>
      {needsKey && <KeyPrompt />}
      <DataViewerModal />
      <RepairBanner />
      {/* On mobile the sidebar drops below the canvas as a collapsible bottom sheet. */}
      <div style={{ display: 'flex', flexDirection: isMobile ? 'column' : 'row', flex: 1, minHeight: 0 }}>
        <ReactFlowProvider>
          <div style={{ flex: 1, minWidth: 0, minHeight: 0 }}>
            <DagCanvas />
          </div>
        </ReactFlowProvider>
        <Sidebar mobile={isMobile} />
      </div>
      {isMobile ? (
        <MobileRunsPanel />
      ) : (
        <div style={{ display: 'flex', height: 260, minHeight: 0, borderTop: '1px solid #27272a' }}>
          <div style={{ flex: 1, minWidth: 0, borderRight: '1px solid #27272a' }}>
            <RunHistory />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <RunDetail />
          </div>
        </div>
      )}
    </div>
  );
}
