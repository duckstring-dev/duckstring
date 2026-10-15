'use client';

import { useEffect } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { usePlaygroundStore } from '@/lib/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';
import { ConsolePanel } from './ConsolePanel';
import { TourCard } from './TourCard';
import { TourSpotlight } from './TourSpotlight';

export function Playground() {
  const isMobile = useIsMobile();
  // ?example=<key> loads an example graph, and ?every=<seconds> (0 = continuous) puts that Refresh on every
  // Outlet, so a setup can be opened straight from a link. (?tide=<seconds> and ?wave=1, from before Refresh,
  // still work.)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const store = usePlaygroundStore.getState();
    const example = params.get('example');
    if (example) store.loadExample(example);
    const ponds = usePlaygroundStore.getState().ponds;
    const outlets = Object.keys(ponds).filter((id) => !Object.values(ponds).some((p) => p.sources.includes(id)));
    let every = parseFloat(params.get('every') ?? params.get('tide') ?? '');
    if (isNaN(every) && params.get('wave') === '1') every = 0;
    if (!isNaN(every) && every >= 0) for (const id of outlets) store.setRefresh(id, { everyMs: every * 1000 });
  }, []);
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
          <div style={{ flex: 1, minWidth: 0, minHeight: 0, position: 'relative' }}>
            <DagCanvas />
            <TourCard />
          </div>
        </ReactFlowProvider>
        <Sidebar mobile={isMobile} />
      </div>
      <ConsolePanel />
      <TourSpotlight />
    </div>
  );
}
