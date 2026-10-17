'use client';

import { useEffect, useMemo } from 'react';
import {
  ReactFlow,
  Background,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  Panel,
  getSmoothStepPath,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type EdgeTypes,
  type Node,
  type NodeTypes,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { THEME_BRAND } from '@/lib/store';
import { changeRows, useTrickleStore, type PondKey } from '@/lib/trickle/store';
import { useTrickleTour } from '@/lib/trickle/tour';
import { useIsMobile } from '@/lib/useIsMobile';
import { TrickleControls } from './TrickleControls';
import { NODE_WIDTH, TrickleNode, nodeHeight } from './TrickleNode';
import { MONO } from './theme';

// The pipeline as a graph: orders and catalog feed priced, which feeds revenue. An edge animates while the
// change it carries flows, labelled with how many rows that change is.

const nodeTypes: NodeTypes = { pond: TrickleNode };

function ChangeEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps) {
  const from = (data as { from: PondKey; activeIn: string }).from;
  const activeIn = (data as { activeIn: string }).activeIn;
  const active = useTrickleStore((s) => s.phase === activeIn);
  const rows = useTrickleStore((s) => (s.result && !s.result.bootstrap ? changeRows(s.result[from]) : null));
  const full = useTrickleStore((s) => (s.result?.bootstrap ? s.result[from].size : null));
  const [path, lx, ly] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 12 });
  const label = full !== null ? `all ${full.toLocaleString()} rows` : rows !== null ? `Δ ${rows} rows` : null;
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        interactionWidth={0}
        style={{
          stroke: active ? THEME_BRAND : '#4a4a55',
          strokeWidth: active ? 3 : 2,
          strokeDasharray: active ? '6 6' : undefined,
          animation: active ? 'ds-flow 0.6s linear infinite' : undefined,
        }}
      />
      {active && label && (
        <EdgeLabelRenderer>
          <div
            data-testid={`edge-${from}`}
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${lx}px, ${ly}px)`,
              background: '#0f0f14',
              border: `1px solid ${THEME_BRAND}`,
              color: THEME_BRAND,
              fontFamily: MONO,
              fontSize: 11,
              padding: '2px 7px',
              borderRadius: 999,
              whiteSpace: 'nowrap',
              pointerEvents: 'none',
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes: EdgeTypes = { change: ChangeEdge };

const EDGES: Edge[] = [
  { id: 'orders-priced', source: 'orders', target: 'priced', type: 'change', data: { from: 'orders', activeIn: 'join' } },
  { id: 'catalog-priced', source: 'catalog', target: 'priced', type: 'change', data: { from: 'catalog', activeIn: 'join' } },
  { id: 'priced-revenue', source: 'priced', target: 'revenue', type: 'change', data: { from: 'priced', activeIn: 'aggregate' } },
];

// Positions from the tables' current sizes, so a growing table never runs into the next node.
function layout(rows: Record<PondKey, number>, vertical: boolean): Node[] {
  const h = (k: PondKey) => nodeHeight(rows[k]);
  const gap = 70;
  const pos: Record<PondKey, { x: number; y: number }> = vertical
    ? {
        catalog: { x: 0, y: 0 },
        orders: { x: NODE_WIDTH + 30, y: 0 },
        priced: { x: (NODE_WIDTH + 30) / 2, y: Math.max(h('catalog'), h('orders')) + gap },
        revenue: { x: (NODE_WIDTH + 30) / 2, y: Math.max(h('catalog'), h('orders')) + gap + h('priced') + gap },
      }
    : {
        catalog: { x: 0, y: 0 },
        orders: { x: 0, y: h('catalog') + 40 },
        priced: { x: NODE_WIDTH + 110, y: (h('catalog') + 40) / 2 },
        revenue: { x: 2 * (NODE_WIDTH + 110), y: (h('catalog') + 40) / 2 },
      };
  return (Object.keys(pos) as PondKey[]).map((k) => ({
    id: k,
    type: 'pond',
    position: pos[k],
    data: { pond: k, vertical },
    draggable: false,
    selectable: false,
  }));
}

export function TrickleCanvas() {
  const isMobile = useIsMobile();
  const { fitView } = useReactFlow();
  const sizes = useTrickleStore((s) => {
    const r = s.result;
    const rows = (k: PondKey) => r?.[k].ids.length ?? 0;
    return `${rows('orders')},${rows('catalog')},${rows('priced')},${rows('revenue')}`;
  });
  const nodes = useMemo(() => {
    const [orders, catalog, priced, revenue] = sizes.split(',').map(Number);
    return layout({ orders: Math.max(orders, 900), catalog: Math.max(catalog, 120), priced: Math.max(priced, 900), revenue: Math.max(revenue, 120) }, isMobile);
  }, [sizes, isMobile]);

  // Frame the graph in the canvas the tour card leaves free, and again when the orientation flips (the
  // fitView prop only applies at the first render). The top keeps clear of the play controls and phase label.
  const inset = useTrickleTour((t) => t.inset);
  useEffect(() => {
    const padding = { top: `${inset.top + 96}px`, bottom: `${inset.bottom + 16}px`, left: '5%', right: '5%' } as const;
    const t = setTimeout(() => fitView({ padding, duration: 300 }), 80);
    return () => clearTimeout(t);
  }, [isMobile, fitView, inset.top, inset.bottom]);

  return (
    <div id="trickle-canvas" style={{ width: '100%', height: '100%', background: '#0f0f14' }}>
      <ReactFlow
        nodes={nodes}
        edges={EDGES}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        minZoom={0.2}
        maxZoom={3}
        proOptions={{ hideAttribution: true }}
        style={{ background: '#0f0f14' }}
      >
        <Background color="#2a2a35" gap={24} size={1} />
        <Panel position="top-left">
          <TrickleControls />
        </Panel>
        <Controls showInteractive={false} style={{ display: isMobile ? 'none' : undefined, background: '#1a1a1f', border: '1px solid #3f3f46', borderRadius: 6 }} />
      </ReactFlow>
    </div>
  );
}
