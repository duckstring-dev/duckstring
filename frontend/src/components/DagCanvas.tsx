'use client';

import { useMemo, useCallback } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
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

import { usePlaygroundStore, getDemandVisualState, DEMAND_COLORS } from '@/lib/store';
import { getLeaves, getRoots } from '@/lib/graph';
import { computeLayout } from '@/lib/layout';
import { PondNode } from './PondNode';
import { RippleNode } from './RippleNode';
import { TriggerNode } from './TriggerNode';

// ─── Custom edges ────────────────────────────────────────────────────────────

function RippleEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const sourceRippleId = (data as { sourceRippleId: string }).sourceRippleId;
  const sinkRippleId = (data as { sinkRippleId: string }).sinkRippleId;

  const rippleState = usePlaygroundStore((s) => s.rippleStates[sourceRippleId]);
  const demandOnSource = rippleState
    ? rippleState.demand.filter((d) => d.sinkId === sinkRippleId)
    : [];
  const demandState = getDemandVisualState(demandOnSource);
  const color = DEMAND_COLORS[demandState];

  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} style={{ stroke: color, strokeWidth: 2 }} />;
}

function PondEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const sourcePondId = (data as { sourcePondId: string }).sourcePondId;
  const sinkPondId = (data as { sinkPondId: string }).sinkPondId;

  const ripples = usePlaygroundStore((s) => s.ripples);
  const rippleStates = usePlaygroundStore((s) => s.rippleStates);

  const color = useMemo(() => {
    const sourceLeaves = getLeaves(sourcePondId, ripples);
    const sinkRoots = getRoots(sinkPondId, ripples);
    const sinkRootIds = new Set(sinkRoots.map((r) => r.id));

    const relevantDemand = sourceLeaves.flatMap((leaf) => {
      const rs = rippleStates[leaf.id];
      if (!rs) return [];
      return rs.demand.filter((d) => d.sinkId !== null && sinkRootIds.has(d.sinkId as string));
    });

    return DEMAND_COLORS[getDemandVisualState(relevantDemand)];
  }, [sourcePondId, sinkPondId, ripples, rippleStates]);

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
        ponds: Object.values(ponds).map((p) => ({ id: p.id, sources: p.sources })),
        ripples: Object.values(ripples).map((r) => ({ id: r.id, parents: r.parents, pondId: r.pondId })),
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
