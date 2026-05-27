import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { PondId, RippleId, Pond, Ripple, ActiveTrigger } from './types';

const RIPPLE_W = 140;
const RIPPLE_H = 60;
const POND_PAD_TOP = 48;
const POND_PAD_SIDE = 24;
const POND_PAD_BOTTOM = 24;
const MIN_POND_W = 160;
const MIN_POND_H = 120;

const TRIGGER_W = 120;
const TRIGGER_H = 36;

interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
}

function buildRippleLayout(
  pondId: PondId,
  ripples: Record<RippleId, Ripple>
): { positions: Record<RippleId, { x: number; y: number }>; width: number; height: number } {
  const pondRipples = Object.values(ripples).filter((r) => r.pondId === pondId);

  if (pondRipples.length === 0) {
    return { positions: {}, width: MIN_POND_W, height: MIN_POND_H };
  }

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', ranksep: 60, nodesep: 40, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const r of pondRipples) {
    g.setNode(r.id, { width: RIPPLE_W, height: RIPPLE_H });
  }
  for (const r of pondRipples) {
    for (const pid of r.parents) {
      if (ripples[pid]?.pondId === pondId) {
        g.setEdge(pid, r.id);
      }
    }
  }

  dagre.layout(g);

  const positions: Record<RippleId, { x: number; y: number }> = {};
  let maxX = 0;
  let maxY = 0;

  for (const r of pondRipples) {
    const n = g.node(r.id);
    // dagre gives center position; convert to top-left
    const x = n.x - RIPPLE_W / 2;
    const y = n.y - RIPPLE_H / 2;
    positions[r.id] = { x, y };
    maxX = Math.max(maxX, n.x + RIPPLE_W / 2);
    maxY = Math.max(maxY, n.y + RIPPLE_H / 2);
  }

  const contentW = maxX;
  const contentH = maxY;

  return {
    positions,
    width: Math.max(contentW + POND_PAD_SIDE * 2, MIN_POND_W),
    height: Math.max(contentH + POND_PAD_TOP + POND_PAD_BOTTOM, MIN_POND_H),
  };
}

export function computeLayout(
  ponds: Record<PondId, Pond>,
  ripples: Record<RippleId, Ripple>,
  triggers: Record<PondId, ActiveTrigger>
): LayoutResult {
  const pondList = Object.values(ponds);

  // Step 1: compute internal ripple layout per pond
  const pondLayouts: Record<
    PondId,
    { positions: Record<RippleId, { x: number; y: number }>; width: number; height: number }
  > = {};
  for (const pond of pondList) {
    pondLayouts[pond.id] = buildRippleLayout(pond.id, ripples);
  }

  // Step 2: compute pond-level layout
  const pg = new dagre.graphlib.Graph();
  pg.setGraph({ rankdir: 'LR', ranksep: 120, nodesep: 80, marginx: 40, marginy: 40 });
  pg.setDefaultEdgeLabel(() => ({}));

  for (const pond of pondList) {
    const { width, height } = pondLayouts[pond.id];
    pg.setNode(pond.id, { width, height });
  }
  for (const pond of pondList) {
    for (const sourceId of pond.sources) {
      if (ponds[sourceId]) {
        pg.setEdge(sourceId, pond.id);
      }
    }
  }

  dagre.layout(pg);

  // Step 3: assemble React Flow nodes and edges
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  for (const pond of pondList) {
    const pn = pg.node(pond.id);
    const { width, height } = pondLayouts[pond.id];
    const pondX = pn.x - width / 2;
    const pondY = pn.y - height / 2;

    nodes.push({
      id: pond.id,
      type: 'pond',
      position: { x: pondX, y: pondY },
      data: { pondId: pond.id },
      style: { width, height },
    });

    // Ripple nodes inside this pond (positions relative to pond)
    const { positions } = pondLayouts[pond.id];
    const pondRipples = Object.values(ripples).filter((r) => r.pondId === pond.id);
    for (const r of pondRipples) {
      const pos = positions[r.id] ?? { x: POND_PAD_SIDE, y: POND_PAD_TOP };
      nodes.push({
        id: r.id,
        type: 'ripple',
        parentId: pond.id,
        position: { x: pos.x + POND_PAD_SIDE, y: pos.y + POND_PAD_TOP },
        data: { rippleId: r.id },
        extent: 'parent',
        draggable: false,
      });
    }

    // Intra-pond ripple edges
    for (const r of pondRipples) {
      for (const pid of r.parents) {
        if (ripples[pid]?.pondId === pond.id) {
          edges.push({
            id: `re-${pid}-${r.id}`,
            source: pid,
            target: r.id,
            type: 'rippleEdge',
            data: { sourceRippleId: pid, sinkRippleId: r.id },
          });
        }
      }
    }

    // Trigger node (positioned to the right of the pond)
    const triggerInfo = triggers[pond.id];
    if (triggerInfo) {
      const triggerId = `trigger-${pond.id}`;
      nodes.push({
        id: triggerId,
        type: 'trigger',
        position: { x: pondX + width + 40, y: pondY + height / 2 - TRIGGER_H / 2 },
        data: { pondId: pond.id },
        style: { width: TRIGGER_W, height: TRIGGER_H },
      });
      edges.push({
        id: `te-${pond.id}`,
        source: triggerId,
        target: pond.id,
        targetHandle: 'trigger-in',
        type: 'triggerEdge',
        data: { pondId: pond.id },
      });
    }
  }

  // Pond-to-pond edges
  for (const pond of pondList) {
    for (const sourceId of pond.sources) {
      if (ponds[sourceId]) {
        edges.push({
          id: `pe-${sourceId}-${pond.id}`,
          source: sourceId,
          sourceHandle: 'out',
          target: pond.id,
          targetHandle: 'in',
          type: 'pondEdge',
          data: { sourcePondId: sourceId, sinkPondId: pond.id },
        });
      }
    }
  }

  return { nodes, edges };
}
