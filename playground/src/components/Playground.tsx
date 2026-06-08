'use client';

import { useEffect } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { usePlaygroundStore } from '@/lib/store';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';
import { ConsolePanel } from './ConsolePanel';

export function Playground() {
  useEffect(() => {
    // Real-time driver. Sim time advances by (real elapsed × speed); frozen while paused.
    let last = performance.now();
    const id = setInterval(() => {
      const real = performance.now();
      const dt = real - last;
      last = real;
      const { paused, speed, now, tick } = usePlaygroundStore.getState();
      if (paused) return;
      tick(now + dt * speed);
    }, 100);
    return () => clearInterval(id);
  }, []);

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
      <ConsolePanel />
    </div>
  );
}
