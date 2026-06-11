'use client';

import { useEffect } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { usePlaygroundStore } from '@/lib/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';
import { ConsolePanel } from './ConsolePanel';

export function Playground() {
  const isMobile = useIsMobile();
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
      <ConsolePanel />
    </div>
  );
}
