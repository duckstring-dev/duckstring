'use client';

import { useEffect, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useLiveStore } from '@/lib/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';
import { RunHistory } from './RunHistory';
import { RunDetail } from './RunDetail';

const POLL_MS = 1000;

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

export function App() {
  const refresh = useLiveStore((s) => s.refresh);
  const isMobile = useIsMobile();

  useEffect(() => {
    // Poll the Catchment for live engine state. A guard prevents overlap if a tick runs long.
    let alive = true;
    let inflight = false;
    const tick = async () => {
      if (inflight) return;
      inflight = true;
      try {
        await refresh();
      } finally {
        inflight = false;
      }
    };
    tick();
    const id = setInterval(() => {
      if (alive) tick();
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [refresh]);

  return (
    <div className="ds-app" style={{ display: 'flex', flexDirection: 'column', width: '100%', overflow: 'hidden', fontFamily: 'ui-monospace, SFMono-Regular, monospace' }}>
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
