'use client';

import { useMemo, useCallback } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  Panel,
  BaseEdge,
  getStraightPath,
  type NodeTypes,
  type EdgeTypes,
  type Connection,
  type EdgeProps,
  type NodeChange,
  type EdgeChange,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { usePlaygroundStore, getDemandEdgeColor } from '@/lib/store';
import { computeLayout } from '@/lib/layout';
import { PondNode } from './PondNode';
import { RippleNode } from './RippleNode';
import { TriggerNode } from './TriggerNode';
import { SimControls } from './SimControls';

// ─── Custom edges ────────────────────────────────────────────────────────────

// Edge colour reflects the SINK's demand on this edge: blue=push, green=pull, grey=none.
function RippleEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const sinkRippleId = (data as { sinkRippleId: string }).sinkRippleId;
  const sinkPull = usePlaygroundStore((s) => s.rippleStates[sinkRippleId]?.hasPull ?? false);
  const sinkPush = usePlaygroundStore((s) => s.rippleStates[sinkRippleId]?.targetF ?? null);

  const color = getDemandEdgeColor(sinkPull, sinkPush);

  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} style={{ stroke: color, strokeWidth: 2 }} />;
}

function PondEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const sinkPondId = (data as { sinkPondId: string }).sinkPondId;
  // A Pond's inbound demand on a Source is captured by the Pond's own pull/push state.
  const sinkPull = usePlaygroundStore((s) => {
    const ps = s.pondStates[sinkPondId];
    return (ps?.hasPull ?? false) || (ps?.hasReceivedPull ?? false);
  });
  const sinkPush = usePlaygroundStore((s) => s.pondStates[sinkPondId]?.targetF ?? null);

  const color = getDemandEdgeColor(sinkPull, sinkPush);

  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} style={{ stroke: color, strokeWidth: 2 }} />;
}

function TriggerEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const pondId = (data as { pondId: string }).pondId;
  const trigger = usePlaygroundStore((s) => s.triggers[pondId]);
  const color = trigger?.kind === 'wave' ? '#22c55e' : '#3b82f6';
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return (
    <BaseEdge
      id={id}
      path={edgePath}
      style={{ stroke: color, strokeWidth: 2, strokeDasharray: '6 3' }}
    />
  );
}

// ─── Node & edge type maps ───────────────────────────────────────────────────

const nodeTypes: NodeTypes = {
  pond: PondNode as NodeTypes[string],
  ripple: RippleNode as NodeTypes[string],
  trigger: TriggerNode as NodeTypes[string],
};

const edgeTypes: EdgeTypes = {
  rippleEdge: RippleEdge as EdgeTypes[string],
  pondEdge: PondEdge as EdgeTypes[string],
  triggerEdge: TriggerEdge as EdgeTypes[string],
};

// ─── Main canvas ─────────────────────────────────────────────────────────────

export function DagCanvas() {
  const ponds = usePlaygroundStore((s) => s.ponds);
  const ripples = usePlaygroundStore((s) => s.ripples);
  const triggers = usePlaygroundStore((s) => s.triggers);
  const linkPonds = usePlaygroundStore((s) => s.linkPonds);
  const linkRipples = usePlaygroundStore((s) => s.linkRipples);
  const clearSelection = usePlaygroundStore((s) => s.clearSelection);

  // Recompute layout whenever graph structure changes (not on every sim tick)
  const layoutKey = useMemo(
    () =>
      JSON.stringify({
        ponds: Object.values(ponds).map((p) => ({ id: p.id, sources: p.sources, name: p.name })),
        // name affects ripple width, so reflow on rename too
        ripples: Object.values(ripples).map((r) => ({ id: r.id, parents: r.parents, pondId: r.pondId, name: r.name })),
        triggers: Object.keys(triggers),
      }),
    [ponds, ripples, triggers]
  );

  const { nodes, edges } = useMemo(
    () => computeLayout(ponds, ripples, triggers),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layoutKey]
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const { source, target } = connection;
      if (!source || !target) return;

      if (ponds[source] && ponds[target]) {
        linkPonds(source, target);
      } else if (ripples[source] && ripples[target]) {
        linkRipples(source, target);
      }
    },
    [ponds, ripples, linkPonds, linkRipples]
  );

  // React Flow v12 controlled mode requires these handlers; we manage layout externally so no-op.
  const onNodesChange = useCallback((_: NodeChange[]) => {}, []);
  const onEdgesChange = useCallback((_: EdgeChange[]) => {}, []);

  return (
    <div style={{ width: '100%', height: '100%', background: '#0f0f14' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onConnect={onConnect}
        onPaneClick={clearSelection}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesDraggable={false}
        nodesConnectable
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        style={{ background: '#0f0f14' }}
      >
        <Background color="#2a2a35" gap={24} size={1} />
        <Panel position="top-left">
          <SimControls />
        </Panel>
        <Controls
          style={{
            background: '#1a1a1f',
            border: '1px solid #3f3f46',
            borderRadius: 6,
          }}
        />
      </ReactFlow>
    </div>
  );
}
