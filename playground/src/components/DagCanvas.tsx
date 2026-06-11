'use client';

import { useMemo, useCallback, useEffect } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  Panel,
  BaseEdge,
  getStraightPath,
  useReactFlow,
  useUpdateNodeInternals,
  type NodeTypes,
  type EdgeTypes,
  type Connection,
  type EdgeProps,
  type NodeChange,
  type EdgeChange,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { usePlaygroundStore, consumeEdgeColor, formatAge, pushTargetF, THEME_PULL, THEME_PUSH } from '@/lib/store';
import { useIsMobile } from '@/lib/useIsMobile';
import { computeLayout, statsLineWidth, type ContentFloors } from '@/lib/layout';
import { PondNode } from './PondNode';
import { RippleNode } from './RippleNode';
import { TriggerNode } from './TriggerNode';
import { SimControls } from './SimControls';

// ─── Custom edges (colour reflects what the sink can consume) ────────────────

function RippleEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const { sourceRippleId, sinkRippleId } = data as { sourceRippleId: string; sinkRippleId: string };
  const parentEndF = usePlaygroundStore((s) => s.rippleStates[sourceRippleId]?.endF ?? 0);
  const childStartF = usePlaygroundStore((s) => s.rippleStates[sinkRippleId]?.startF ?? 0);
  const childTargetF = usePlaygroundStore((s) => pushTargetF(s.rippleStates[sinkRippleId]?.targets ?? []));
  const color = consumeEdgeColor(parentEndF, childStartF, childTargetF);
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} interactionWidth={0} style={{ stroke: color, strokeWidth: 2 }} />;
}

function PondEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const { sourcePondId, sinkPondId } = data as { sourcePondId: string; sinkPondId: string };
  const parentEndF = usePlaygroundStore((s) => s.pondStates[sourcePondId]?.endF ?? 0);
  const childStartF = usePlaygroundStore((s) => s.pondStates[sinkPondId]?.startF ?? 0);
  const childTargetF = usePlaygroundStore((s) => pushTargetF(s.pondStates[sinkPondId]?.targets ?? []));
  const color = consumeEdgeColor(parentEndF, childStartF, childTargetF);
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} interactionWidth={0} style={{ stroke: color, strokeWidth: 2 }} />;
}

function TriggerEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const pondId = (data as { pondId: string }).pondId;
  const trigger = usePlaygroundStore((s) => s.triggers[pondId]);
  const color = trigger?.kind === 'wave' ? THEME_PULL : THEME_PUSH;
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return (
    <BaseEdge
      id={id}
      path={edgePath}
      interactionWidth={0}
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
  const pondStates = usePlaygroundStore((s) => s.pondStates);
  const rippleStates = usePlaygroundStore((s) => s.rippleStates);
  const now = usePlaygroundStore((s) => s.now);
  const linkPonds = usePlaygroundStore((s) => s.linkPonds);
  const linkRipples = usePlaygroundStore((s) => s.linkRipples);
  const clearSelection = usePlaygroundStore((s) => s.clearSelection);
  const selectedPondId = usePlaygroundStore((s) => s.selectedPondId);
  const selectedRippleId = usePlaygroundStore((s) => s.selectedRippleId);
  const selectedTriggerId = usePlaygroundStore((s) => s.selectedTriggerId);

  // On mobile, tapping a node zooms to it — the clear "this is selected" signal, and the
  // only way the node text gets readable. The delay lets the bottom sheet open (the canvas
  // shrinks) before the viewport is fitted to the remaining space.
  const { fitView } = useReactFlow();
  const updateNodeInternals = useUpdateNodeInternals();
  const isMobile = useIsMobile();
  useEffect(() => {
    if (!isMobile) return;
    const id = selectedRippleId ?? (selectedTriggerId ? `trigger-${selectedTriggerId}` : selectedPondId);
    if (!id) return;
    const t = setTimeout(() => fitView({ nodes: [{ id }], duration: 350, padding: 0.15, maxZoom: 1.1 }), 120);
    return () => clearTimeout(t);
  }, [isMobile, selectedPondId, selectedRippleId, selectedTriggerId, fitView]);

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

  // Each box's minimum width to fit its live stats line (grows with run counts / longer ages).
  const floors = useMemo<ContentFloors>(() => {
    const r: Record<string, number> = {};
    for (const rp of Object.values(ripples)) {
      const rs = rippleStates[rp.id];
      if (!rs) continue;
      const startedF = rs.isRunning ? rs.startF : rs.endF;
      r[rp.id] = statsLineWidth({
        pushAge: pushTargetF(rs.targets) != null ? formatAge(pushTargetF(rs.targets)!, now) : null,
        startAge: formatAge(startedF, now),
        startCount: rs.runsStarted,
        endAge: formatAge(rs.endF, now),
        endCount: rs.runsCompleted,
        pad: 20,
      });
    }
    const p: Record<string, number> = {};
    for (const pd of Object.values(ponds)) {
      const ps = pondStates[pd.id];
      if (!ps) continue;
      p[pd.id] = statsLineWidth({
        pushAge: pushTargetF(ps.targets) != null ? formatAge(pushTargetF(ps.targets)!, now) : null,
        startAge: formatAge(ps.startF, now),
        startCount: ps.runsStarted,
        endAge: formatAge(ps.endF, now),
        endCount: ps.runsCompleted,
        pad: 24,
      });
    }
    return { ripples: r, ponds: p };
  }, [ponds, ripples, pondStates, rippleStates, now]);

  // statsLineWidth buckets to 8px, so this key only changes when a box actually needs resizing —
  // keeping the (expensive, position-shifting) dagre relayout off the per-tick path.
  const widthKey = useMemo(() => {
    const enc = (m: Record<string, number>) =>
      Object.entries(m)
        .map(([k, v]) => `${k}:${v}`)
        .sort()
        .join(',');
    return `${enc(floors.ripples ?? {})}|${enc(floors.ponds ?? {})}`;
  }, [floors]);

  const { nodes, edges } = useMemo(
    () => computeLayout(ponds, ripples, triggers, floors, isMobile ? 'TB' : 'LR'),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layoutKey, widthKey, isMobile]
  );

  // Handle positions move between the LR and TB layouts; nudge React Flow to re-measure them
  // when the orientation flips, or edges keep their old anchors. Then re-frame: the fitView prop
  // only fires at init, against the pre-hydration desktop layout (isMobile is false during SSR).
  useEffect(() => {
    updateNodeInternals(nodes.map((n) => n.id));
    const t = setTimeout(() => fitView({ padding: 0.15 }), 100);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile]);

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
