'use client';

import { useEffect } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useLiveStore } from '@/lib/store';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';
import { RunHistory } from './RunHistory';

const POLL_MS = 1000;

export function App() {
  const refresh = useLiveStore((s) => s.refresh);

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
    <div style={{ display: 'flex', flexDirection: 'column', width: '100vw', height: '100vh', overflow: 'hidden', fontFamily: 'ui-monospace, SFMono-Regular, monospace' }}>
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <ReactFlowProvider>
          <div style={{ flex: 1, minWidth: 0 }}>
            <DagCanvas />
          </div>
        </ReactFlowProvider>
        <Sidebar />
      </div>
      <RunHistory />
    </div>
  );
}
