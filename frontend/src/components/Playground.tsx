'use client';

import { useEffect } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { usePlaygroundStore } from '@/lib/store';
import { DagCanvas } from './DagCanvas';
import { Sidebar } from './Sidebar';

export function Playground() {
  const tick = usePlaygroundStore((s) => s.tick);

  useEffect(() => {
    const id = setInterval(() => tick(Date.now()), 100);
    return () => clearInterval(id);
  }, [tick]);

  return (
    <div style={{ display: 'flex', width: '100vw', height: '100vh', overflow: 'hidden', fontFamily: 'ui-monospace, SFMono-Regular, monospace' }}>
      <ReactFlowProvider>
        <div style={{ flex: 1, minWidth: 0 }}>
          <DagCanvas />
        </div>
      </ReactFlowProvider>
      <Sidebar />
    </div>
  );
}
