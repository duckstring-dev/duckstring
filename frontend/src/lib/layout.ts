import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { PondId, RippleId, Pond, Ripple, TriggerView } from './types';

const MIN_RIPPLE_W = 200;
const RIPPLE_H = 80;

// Width of the demand + freshness stats line, estimated from its live contents so the box can grow
// as run counts and ages get longer. Mirrors the layout of PondNode/RippleNode's second line:
//   [● dot][≤box]  ↑{startAge} ({startCount})  ✓{endAge} ({endCount})
// `pushAge` is the formatted push-target age, or null for the "≤—" placeholder. `pad` is the node's
// horizontal padding. Rounded up to a multiple of 8 so small age changes don't jitter the layout.
export function statsLineWidth(opts: {
  pushAge: string | null;
  startAge: string;
  startCount: number;
  endAge: string;
  endCount: number;
  pad: number;
}): number {
  const CH11 = 6.7; // monospace char px at fontSize 11
  const CH9 = 5.5; // at fontSize 9 (the ≤ box)
  const dot = 9; // 7px dot + 1px border
  const boxText = opts.pushAge != null ? `≤${opts.pushAge}` : '≤—';
  const box = boxText.length * CH9 + 8; // padding 3*2 + border 1*2
  const demand = dot + 4 /* gap */ + box;
  const start = `↑${opts.startAge} (${opts.startCount})`.length * CH11;
  const end = `✓${opts.endAge} (${opts.endCount})`.length * CH11;
  const total = demand + 6 /* gap */ + start + 6 /* gap */ + end + opts.pad + 4 /* slack */;
  return Math.ceil(total / 8) * 8;
}

// Width sized to fit the ripple name on its own line; floored to its live stats line.
function rippleWidth(r: Ripple, floor = 0): number {
  const nameW = r.name.length * 7.2; // ~13px monospace
  return Math.max(MIN_RIPPLE_W, floor, Math.ceil(nameW + 20 /* padding */));
}

// Minimum pond width to fit its name on the title line (the stats line is floored separately).
function pondNameWidth(name: string): number {
  return Math.ceil(name.length * 8.2 + 24 /* padding */); // ~13px bold monospace
}
const POND_PAD_TOP = 68; // header area above the ripples (also the height of a ripple-less Pond)
const POND_PAD_SIDE = 24;
const POND_PAD_BOTTOM = 24;
// A Pond is always at least as wide as one ripple plus its side margins — the floor a single-ripple
// Pond would have. Applied even to a Pond with no ripples (a Draw) so it doesn't render narrower.
const MIN_POND_W = MIN_RIPPLE_W + POND_PAD_SIDE * 2;
const MIN_POND_H = 120;

const TRIGGER_W = 120;
const TRIGGER_H = 36;

interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
}

// Per-node minimum content widths (px), derived from live state in DagCanvas so boxes grow to fit
// their stats line. Both maps are optional/sparse; missing entries fall back to name-based sizing.
export interface ContentFloors {
  ripples?: Record<RippleId, number>;
  ponds?: Record<PondId, number>;
}

function buildRippleLayout(
  pondId: PondId,
  ripples: Record<RippleId, Ripple>,
  rippleFloors?: Record<RippleId, number>,
  direction: 'LR' | 'TB' = 'LR'
): {
  positions: Record<RippleId, { x: number; y: number }>;
  widths: Record<RippleId, number>;
  width: number;
  height: number;
} {
  const pondRipples = Object.values(ripples).filter((r) => r.pondId === pondId);

  if (pondRipples.length === 0) {
    // A ripple-less Pond (a Draw) is just its header — no ripple area, no bottom padding.
    return { positions: {}, widths: {}, width: MIN_POND_W, height: POND_PAD_TOP };
  }

  const widths: Record<RippleId, number> = {};
  for (const r of pondRipples) widths[r.id] = rippleWidth(r, rippleFloors?.[r.id] ?? 0);

  const g = new dagre.graphlib.Graph();
  // TB: tighter gaps — the vertical flow is rank-to-rank (ranksep) and siblings sit
  // side by side (nodesep), which is what widens the pond on a narrow screen.
  const gaps = direction === 'TB' ? { ranksep: 32, nodesep: 24 } : { ranksep: 60, nodesep: 40 };
  g.setGraph({ rankdir: direction, ...gaps, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const r of pondRipples) {
    g.setNode(r.id, { width: widths[r.id], height: RIPPLE_H });
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
    const w = widths[r.id];
    // dagre gives center position; convert to top-left
    const x = n.x - w / 2;
    const y = n.y - RIPPLE_H / 2;
    positions[r.id] = { x, y };
    maxX = Math.max(maxX, n.x + w / 2);
    maxY = Math.max(maxY, n.y + RIPPLE_H / 2);
  }

  const contentW = maxX;
  const contentH = maxY;

  return {
    positions,
    widths,
    width: Math.max(contentW + POND_PAD_SIDE * 2, MIN_POND_W),
    height: Math.max(contentH + POND_PAD_TOP + POND_PAD_BOTTOM, MIN_POND_H),
  };
}

// `direction` orients the whole graph: LR on desktop, TB on mobile so the chain (and each pond's
// ripple flow) reads down a portrait screen — it fits at roughly twice the zoom.
export function computeLayout(
  ponds: Record<PondId, Pond>,
  ripples: Record<RippleId, Ripple>,
  triggers: Record<PondId, TriggerView>,
  floors?: ContentFloors,
  direction: 'LR' | 'TB' = 'LR'
): LayoutResult {
  const pondList = Object.values(ponds);
  const vertical = direction === 'TB';

  // Step 1: compute internal ripple layout per pond
  const pondLayouts: Record<
    PondId,
    {
      positions: Record<RippleId, { x: number; y: number }>;
      widths: Record<RippleId, number>;
      width: number;
      height: number;
    }
  > = {};
  for (const pond of pondList) {
    const layout = buildRippleLayout(pond.id, ripples, floors?.ripples, direction);
    // Floor the pond width to fit its title line and its (live) stats line.
    layout.width = Math.max(layout.width, pondNameWidth(pond.name), floors?.ponds?.[pond.id] ?? 0);
    pondLayouts[pond.id] = layout;
  }

  // Step 2: compute pond-level layout
  const pg = new dagre.graphlib.Graph();
  pg.setGraph({ rankdir: direction, ranksep: vertical ? 80 : 120, nodesep: vertical ? 60 : 80, marginx: 40, marginy: 40 });
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
      data: { pondId: pond.id, vertical },
      style: { width, height },
    });

    // Ripple nodes inside this pond (positions relative to pond)
    const { positions, widths } = pondLayouts[pond.id];
    const pondRipples = Object.values(ripples).filter((r) => r.pondId === pond.id);
    for (const r of pondRipples) {
      const pos = positions[r.id] ?? { x: POND_PAD_SIDE, y: POND_PAD_TOP };
      nodes.push({
        id: r.id,
        type: 'ripple',
        parentId: pond.id,
        position: { x: pos.x + POND_PAD_SIDE, y: pos.y + POND_PAD_TOP },
        data: { rippleId: r.id, vertical },
        extent: 'parent',
        draggable: false,
        style: { width: widths[r.id] ?? MIN_RIPPLE_W, height: RIPPLE_H },
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

    // Trigger node — to the right of the pond (LR), or below it (TB, where the right edge is the
    // narrow phone's margin and an outlet's bottom is free).
    const triggerInfo = triggers[pond.id];
    if (triggerInfo) {
      const triggerId = `trigger-${pond.id}`;
      nodes.push({
        id: triggerId,
        type: 'trigger',
        position: vertical
          ? { x: pondX + width / 2 - TRIGGER_W / 2, y: pondY + height + 40 }
          : { x: pondX + width + 40, y: pondY + height / 2 - TRIGGER_H / 2 },
        data: { pondId: pond.id, vertical },
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
