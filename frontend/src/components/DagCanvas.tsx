'use client';

import { useMemo } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  Panel,
  BaseEdge,
  getStraightPath,
  type NodeTypes,
  type EdgeTypes,
  type EdgeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useLiveStore, consumeEdgeColor, formatAge, THEME_PULL, THEME_PUSH, THEME_SUCCESS, THEME_DANGER } from '@/lib/store';
import { computeLayout, statsLineWidth, type ContentFloors } from '@/lib/layout';
import { PondNode } from './PondNode';
import { RippleNode } from './RippleNode';
import { TriggerNode } from './TriggerNode';

// ─── Custom edges (read-only; colour reflects the sink's demand) ─────────────

function RippleEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const { sourceRippleId, sinkRippleId } = data as { sourceRippleId: string; sinkRippleId: string };
  const parentEndF = useLiveStore((s) => s.rippleViews[sourceRippleId]?.endF ?? 0);
  const childStartF = useLiveStore((s) => s.rippleViews[sinkRippleId]?.startF ?? 0);
  const childTargetF = useLiveStore((s) => s.rippleViews[sinkRippleId]?.targetF ?? null);
  const color = consumeEdgeColor(parentEndF, childStartF, childTargetF);
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} style={{ stroke: color, strokeWidth: 2 }} />;
}

function PondEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const { sourcePondId, sinkPondId } = data as { sourcePondId: string; sinkPondId: string };
  const parentEndF = useLiveStore((s) => s.pondViews[sourcePondId]?.endF ?? 0);
  const childStartF = useLiveStore((s) => s.pondViews[sinkPondId]?.startF ?? 0);
  const childTargetF = useLiveStore((s) => s.pondViews[sinkPondId]?.targetF ?? null);
  const color = consumeEdgeColor(parentEndF, childStartF, childTargetF);
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} style={{ stroke: color, strokeWidth: 2 }} />;
}

function TriggerEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const pondId = (data as { pondId: string }).pondId;
  const trigger = useLiveStore((s) => s.triggers[pondId]);
  const color = trigger?.kind === 'wave' ? THEME_PULL : THEME_PUSH;
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return (
    <BaseEdge id={id} path={edgePath} style={{ stroke: color, strokeWidth: 2, strokeDasharray: '6 3' }} />
  );
}

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

// ─── Status / legend panel ───────────────────────────────────────────────────

// Top-left panel: the Duckstring brand mark (and natural home for catchment navigation later),
// over the live connection status.
function StatusPanel() {
  const connected = useLiveStore((s) => s.connected);
  const error = useLiveStore((s) => s.error);
  const count = useLiveStore((s) => Object.keys(s.ponds).length);
  return (
    <div
      style={{
        background: '#15151a',
        border: '1px solid #27272a',
        borderRadius: 8,
        padding: '9px 12px',
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
        fontSize: 11,
        color: '#a1a1aa',
        display: 'flex',
        flexDirection: 'column',
        gap: 7,
        minWidth: 168,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
        <span
          aria-label="Duckstring"
          style={{
            width: 39,
            height: 39,
            flexShrink: 0,
            backgroundImage: 'url(/logo-mark.svg)',
            backgroundSize: 'contain',
            backgroundRepeat: 'no-repeat',
            backgroundPosition: 'center',
          }}
        />
        <span style={{ fontSize: 17, fontWeight: 700, lineHeight: 1, color: '#f4f4f5', letterSpacing: '0.01em' }}>
          Duckstring
        </span>
      </div>
      <div style={{ height: 1, background: '#27272a' }} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <span
          title={connected ? 'connected' : error ? 'unreachable' : 'connecting'}
          style={{ width: 8, height: 8, flexShrink: 0, borderRadius: '50%', background: connected ? THEME_SUCCESS : THEME_DANGER }}
        />
        <span style={{ color: '#71717a' }}>
          Catchment
          <span style={{ color: '#3f3f46' }}> · </span>
          {connected ? `${count} pond${count === 1 ? '' : 's'}` : error ? 'unreachable' : 'connecting…'}
        </span>
      </div>
    </div>
  );
}

// ─── Main canvas ─────────────────────────────────────────────────────────────

export function DagCanvas() {
  const ponds = useLiveStore((s) => s.ponds);
  const ripples = useLiveStore((s) => s.ripples);
  const triggers = useLiveStore((s) => s.triggers);
  const pondViews = useLiveStore((s) => s.pondViews);
  const rippleViews = useLiveStore((s) => s.rippleViews);
  const now = useLiveStore((s) => s.now);
  const clearSelection = useLiveStore((s) => s.clearSelection);

  // Relayout only when graph structure changes — not on every poll.
  const layoutKey = useMemo(
    () =>
      JSON.stringify({
        ponds: Object.values(ponds).map((p) => ({ id: p.id, sources: p.sources, name: p.name })),
        ripples: Object.values(ripples).map((r) => ({ id: r.id, parents: r.parents, pondId: r.pondId, name: r.name })),
        triggers: Object.keys(triggers),
      }),
    [ponds, ripples, triggers]
  );

  // Each box's minimum width to fit its live stats line (grows with run counts / longer ages).
  const floors = useMemo<ContentFloors>(() => {
    const r: Record<string, number> = {};
    for (const rp of Object.values(ripples)) {
      const rs = rippleViews[rp.id];
      if (!rs) continue;
      const startedF = rs.status === 'running' ? rs.startF : rs.endF;
      r[rp.id] = statsLineWidth({
        pushAge: rs.targetF != null ? formatAge(rs.targetF, now) : null,
        startAge: formatAge(startedF, now),
        startCount: rs.runsStarted,
        endAge: formatAge(rs.endF, now),
        endCount: rs.runsCompleted,
        pad: 20,
      });
    }
    const p: Record<string, number> = {};
    for (const pd of Object.values(ponds)) {
      const ps = pondViews[pd.id];
      if (!ps) continue;
      p[pd.id] = statsLineWidth({
        pushAge: ps.targetF != null ? formatAge(ps.targetF, now) : null,
        startAge: formatAge(ps.startF, now),
        startCount: ps.runsStarted,
        endAge: formatAge(ps.endF, now),
        endCount: ps.runsCompleted,
        pad: 24,
      });
    }
    return { ripples: r, ponds: p };
  }, [ponds, ripples, pondViews, rippleViews, now]);

  // statsLineWidth buckets to 8px, so this key only changes when a box actually needs resizing —
  // keeping the (position-shifting) dagre relayout off the per-tick path.
  const widthKey = useMemo(() => {
    const enc = (m: Record<string, number>) =>
      Object.entries(m)
        .map(([k, v]) => `${k}:${v}`)
        .sort()
        .join(',');
    return `${enc(floors.ripples ?? {})}|${enc(floors.ponds ?? {})}`;
  }, [floors]);

  const { nodes, edges } = useMemo(
    () => computeLayout(ponds, ripples, triggers, floors),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layoutKey, widthKey]
  );

  // React Flow controlled mode requires these handlers; layout is managed externally, so no-op.
  const onNodesChange = () => {};
  const onEdgesChange = () => {};

  return (
    <div style={{ width: '100%', height: '100%', background: '#0f0f14' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onPaneClick={clearSelection}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        style={{ background: '#0f0f14' }}
      >
        <Background color="#2a2a35" gap={24} size={1} />
        <Panel position="top-left">
          <StatusPanel />
        </Panel>
        <Controls style={{ background: '#1a1a1f', border: '1px solid #3f3f46', borderRadius: 6 }} />
      </ReactFlow>
    </div>
  );
}
