'use client';

import { useEffect } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useTrickleStore } from '@/lib/trickle/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { TrickleCanvas } from './TrickleCanvas';
import { TrickleSidebar } from './TrickleSidebar';
import { TrickleTourCard } from './TrickleTourCard';

// /trickle: the Trickle demo pipeline running in real time, with a guided tour. Same shell as the orchestration
// Playground: the canvas, a sidebar (a bottom sheet on a phone), and the tour card over the canvas.

export function TrickleApp() {
  const isMobile = useIsMobile();

  // Real-time driver: the pipeline advances by elapsed time × speed, frozen while paused.
  useEffect(() => {
    let last = performance.now();
    const id = setInterval(() => {
      const now = performance.now();
      useTrickleStore.getState().tick(now - last);
      last = now;
    }, 50);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="ds-app" style={{ display: 'flex', flexDirection: isMobile ? 'column' : 'row', width: '100%', overflow: 'hidden', fontFamily: 'ui-monospace, SFMono-Regular, monospace' }}>
      <ReactFlowProvider>
        <div style={{ flex: 1, minWidth: 0, minHeight: 0, position: 'relative' }}>
          <TrickleCanvas />
          <TrickleTourCard />
        </div>
      </ReactFlowProvider>
      <TrickleSidebar mobile={isMobile} />
    </div>
  );
}
